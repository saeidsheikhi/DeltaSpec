#!/usr/bin/env python3
"""AppWorld-side worker. Runs **inside `.venv-appworld`**, speaks JSON on stdout.

EffectGate cannot import `appworld` directly: AppWorld pins pydantic 1.x and sharing a
virtualenv would make the Phase A results unreproducible (notes/DECISIONS.md). So all
AppWorld work happens here, in a subprocess, and the adapter in the main venv talks to
it over a one-shot JSON protocol.

    python appworld_worker.py '<json request>'

Requests (`op`):

    survey_task   task metadata, allowed apps, table schemas
    state         normalised state view for one task
    reference_run execute the task's official ground-truth solution, returning the
                  pre-state, post-state, trace and the official evaluator's verdict
    run_code      execute arbitrary agent code, same returns
    replay        snapshot once, then run N candidates each from the *restored*
                  pre-state -- the STATEFUL_REPLAY_SPEC gate loop
    evaluate_task AppWorld's out-of-process official evaluator, for cross-checking
                  the in-process `world.evaluate()` verdict
    api_check     record the installed AppWorld API surface as a result artifact

Nothing here modifies AppWorld or its evaluators; `evaluate()` is called as-is.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

# The field-aware projection must serialise values exactly as the evaluator expects, so
# the canonical form comes from the one implementation in the EffectGate package. That
# module depends on the standard library only, so importing it here -- inside the
# AppWorld virtualenv, which has no pydantic 2 -- is safe.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from effectgate.contracts.fieldview import canonical_type, canonical_value  # noqa: E402

#: Rows per table kept in the field projection. A table over the cap is marked
#: `truncated`, and every record predicate over it is a fail-closed representability
#: error -- never a partial answer. Measured on the diagnostic tasks: the largest
#: owner-scoped table was gmail.UserEmailThread at 352 rows.
FIELD_ROWS_CAP = int(os.getenv("EFFECTGATE_FIELD_ROWS_CAP", "400"))

#: Fields excluded from the state view because they are identity/bookkeeping noise
#: rather than semantic state.
DROP_FIELDS = {"_sa_instance_state"}

#: Tables excluded from the effect state view: they record that the agent *finished*,
#: not what it did to the world. `supervisor.Task` holds the task-completion signal and
#: the submitted answer, and it changes on literally every run.
#:
#: This mirrors AppWorld's own evaluator, which is the justification for doing it:
#: on task b7a9ee9_1 the official test `changed_model_names() == {spotify.UserArtistFollowing}`
#: PASSES even though `supervisor.Task` was also modified. Including it would make
#: every "only X changed" contract fail for a reason that has nothing to do with the
#: agent's effects on the world.
BOOKKEEPING_TABLES = {("supervisor", "Task")}


def _model_names(coll) -> list[str]:
    return sorted(n for n in dir(coll) if not n.startswith("_") and n != "SQLModel")


def _apps(task) -> list[str]:
    return [a for a in (task.allowed_apps or []) if a not in ("api_docs",)]


def _dump(rec) -> dict:
    d = rec.model_dump() if hasattr(rec, "model_dump") else dict(rec)
    return {k: v for k, v in d.items() if k not in DROP_FIELDS}


def normalised_state(world, record_level_apps: list[str] | None = None,
                     field_aware: bool = False) -> dict:
    """A bounded state view suitable for effect contracts.

    The world holds ~365k rows, so a full dump is impossible. Two levels are kept:

    ``counts``  every table in the task's allowed apps -> row count. ~120 integers,
                and it gives *complete* collateral coverage: any unauthorised
                creation or deletion anywhere in scope moves one of these numbers.
    ``records`` per table, ``{row_id: record_hash}``. AppWorld computes `record_hash`
                itself, so this detects in-place field edits that leave counts
                unchanged. Restricted to `record_level_apps` to stay bounded.

    `records` is for evaluation, not for the compiler prompt -- see `prompt_state`.
    """
    mc = world.task.model_collection
    apps = _apps(world.task)
    record_apps = set(record_level_apps if record_level_apps is not None else apps)

    counts: dict = {}
    records: dict = {}
    for app in apps:
        coll = getattr(mc, app, None)
        if coll is None:
            continue
        counts[app] = {}
        if app in record_apps:
            records[app] = {}
        for mname in _model_names(coll):
            if (app, mname) in BOOKKEEPING_TABLES:
                continue
            M = getattr(coll, mname)
            try:
                counts[app][mname] = M.count()
            except Exception:
                continue
            if app in record_apps:
                try:
                    records[app][mname] = {
                        str(getattr(r, "id", i)): getattr(r, "record_hash", None)
                        for i, r in enumerate(M.all())
                    }
                except Exception:
                    records[app][mname] = {}
    state = {"counts": counts, "records": records}
    # Tier 1 is opt-in so the hash-only view stays byte-identical to the frozen baseline.
    if field_aware:
        fields, meta = field_projection(world, relevant_apps(world.task))
        state["fields"] = fields
        state["fields_meta"] = meta
    return state


def _supervisor_user_ids(world, apps: list[str]) -> dict[str, int | None]:
    """The supervisor's own row id in each app's User table.

    Resolved by `email`, falling back to `phone_number` (the `phone` app keys on it).
    Verified on 2026-09-05: resolves in every app for every probed task. `None` means
    the app has no User table or the supervisor is absent from it; the projection then
    has no owner scope there and says so.
    """
    sup = dict(world.task.supervisor)
    mc = world.task.model_collection
    out: dict[str, int | None] = {}
    for app in apps:
        coll = getattr(mc, app, None)
        U = getattr(coll, "User", None) if coll is not None else None
        uid = None
        if U is not None:
            ufields = set(getattr(U, "__fields__", {}))
            for key in ("email", "phone_number"):
                if key in ufields and sup.get(key):
                    rows = [r for r in U.all() if getattr(r, key, None) == sup[key]]
                    if rows:
                        uid = int(rows[0].id)
                        break
        out[app] = uid
    return out


def _table_meta(M) -> dict:
    """Column types and declared foreign keys from the installed SQLModel class."""
    ftypes = {}
    for fname, f in (getattr(M, "__fields__", {}) or {}).items():
        if fname == "record_hash" or fname in DROP_FIELDS:
            continue                      # tier 0 carries the hash; rows never do
        t = getattr(f, "outer_type_", None) or getattr(f, "type_", None)
        ftypes[fname] = canonical_type(getattr(t, "__name__", str(t)))
    fks: dict[str, str] = {}
    tbl = getattr(M, "__table__", None)
    if tbl is not None:
        for c in tbl.columns:
            for fk in c.foreign_keys:
                fks[c.name] = str(fk.column)          # e.g. "users.id", "songs.id"
    owner_cols = [c for c, tgt in fks.items() if tgt.endswith("users.id")]
    return {"field_types": ftypes, "fks": fks, "owner_cols": owner_cols,
            "sql_table": getattr(tbl, "name", None)}


def _row_dict(r) -> dict:
    d = r.model_dump() if hasattr(r, "model_dump") else dict(r)
    return {k: canonical_value(v) for k, v in d.items()
            if k not in DROP_FIELDS and k != "record_hash"}


def field_projection(world, apps: list[str], cap: int = FIELD_ROWS_CAP) -> tuple[dict, dict]:
    """Tier 1 of the state view: typed field values for the supervisor's own rows.

    Scope is a property of the **schema**, not of the task. For every table in ``apps``:

    * ``owner``       rows whose owner FK (a column referencing ``users.id``) is the
                      supervisor's user id;
    * ``owner_child`` rows of tables with no owner column whose FK points at one of the
                      supervisor's owner rows (join/child tables such as
                      `spotify.PlaylistSong` -> `playlists.id`). Found on 2026-09-05: the
                      outward-only hop missed them, and "songs in my playlists" lives
                      there;
    * ``fk_hop``      rows of catalogue tables (`spotify.Song`, `Artist`, ...) reachable by
                      one declared FK from any owner or owner_child row.

    Rows per table are capped; over the cap the table is marked ``truncated`` and record
    predicates over it fail closed.

    Returns ``(fields, meta)``. ``fields[app][Table]`` is self-describing -- it carries
    its own ``field_types`` -- so a snapshot evaluates identically anywhere.
    Design: notes/DECISIONS.md, 2026-09-05 "Field-aware AppWorld representation".
    """
    mc = world.task.model_collection
    uids = _supervisor_user_ids(world, apps)
    fields: dict = {}
    meta: dict = {"supervisor_user_ids": uids, "rows_cap": cap, "apps": list(apps), "tables": {}}
    # SQL table name -> "<app>.<Model>" so declared FKs can be expressed in the DSL's
    # own table vocabulary. AppWorld keeps one database per app and every app names its
    # users table `users`, so resolution is **app-local first** (a FK inside `spotify`
    # targets `spotify.User`), falling back to a global lookup only for names the app
    # itself does not define. (v0.5 used one global map and reported
    # `spotify.Playlist.user_id -> todoist.User`; see notes/FAILURES.md.)
    sql_to_model_by_app: dict[str, dict[str, str]] = {}
    sql_to_model_global: dict[str, str] = {}
    for app in _apps(world.task):
        coll = getattr(mc, app, None)
        if coll is None:
            continue
        for mname in _model_names(coll):
            sql = _table_meta(getattr(coll, mname))["sql_table"]
            if sql:
                sql_to_model_by_app.setdefault(app, {})[sql] = f"{app}.{mname}"
                sql_to_model_global.setdefault(sql, f"{app}.{mname}")

    def _view_fks(tm: dict, app: str) -> dict:
        local = sql_to_model_by_app.get(app, {})
        return {c: local.get(tgt.split(".")[0], sql_to_model_global.get(tgt.split(".")[0], tgt.split(".")[0]))
                for c, tgt in tm["fks"].items()}
    # pass 1: owner-scoped tables, and collect referenced ids for catalogue tables
    referenced: dict[str, set] = {}          # sql table name -> set(ids)
    catalogue: dict[str, tuple[str, str, object, dict]] = {}
    for app in apps:
        coll = getattr(mc, app, None)
        if coll is None:
            continue
        fields[app] = {}
        for mname in _model_names(coll):
            if (app, mname) in BOOKKEEPING_TABLES:
                continue
            M = getattr(coll, mname)
            tm = _table_meta(M)
            uid = uids.get(app)
            if tm["owner_cols"] and uid is not None:
                try:
                    all_rows = M.all()
                except Exception:
                    continue
                rows = [r for r in all_rows if any(getattr(r, oc, None) == uid for oc in tm["owner_cols"])]
                n_total = len(rows)
                rows = rows[:cap]
                view_rows = {str(r.id): _row_dict(r) for r in rows}
                for c, tgt in tm["fks"].items():
                    if tgt.endswith("users.id"):
                        continue
                    tname = tgt.split(".")[0]
                    referenced.setdefault(tname, set()).update(
                        getattr(r, c) for r in rows if getattr(r, c, None) is not None)
                fields[app][mname] = {"scope": "owner", "n_total": n_total,
                                      "n_projected": len(view_rows), "truncated": n_total > cap,
                                      "field_types": tm["field_types"], "primary_key": "id",
                                      "foreign_keys": _view_fks(tm, app), "rows": view_rows}
            else:
                catalogue[f"{app}.{mname}"] = (app, mname, M, tm)
    # pass 1b: child/join tables whose FK points *at* an owner-scoped table
    owner_ids: dict[str, set] = {}            # sql table name -> owner row ids
    for app in apps:
        for mname, v in fields.get(app, {}).items():
            tm_sql = _table_meta(getattr(getattr(mc, app), mname))["sql_table"]
            if tm_sql:
                owner_ids[tm_sql] = {int(i) for i in v["rows"]}
    for key in list(catalogue):
        app, mname, M, tm = catalogue[key]
        parent_cols = {c: tgt.split(".")[0] for c, tgt in tm["fks"].items()
                       if tgt.split(".")[0] in owner_ids and not tgt.endswith("users.id")}
        if not parent_cols:
            continue
        try:
            rows = [r for r in M.all()
                    if any(getattr(r, c, None) in owner_ids[t] for c, t in parent_cols.items())]
        except Exception:
            continue
        n_total = len(rows)
        rows = rows[:cap]
        for c, tgt in tm["fks"].items():
            if tgt.endswith("users.id") or tgt.split(".")[0] in owner_ids:
                continue
            referenced.setdefault(tgt.split(".")[0], set()).update(
                getattr(r, c) for r in rows if getattr(r, c, None) is not None)
        fields[app][mname] = {"scope": "owner_child", "n_total": n_total,
                              "n_projected": len(rows), "truncated": n_total > cap,
                              "field_types": tm["field_types"], "primary_key": "id",
                              "foreign_keys": _view_fks(tm, app),
                              "rows": {str(r.id): _row_dict(r) for r in rows}}
        del catalogue[key]
    # pass 2: catalogue tables reachable by one FK hop from owner / owner_child rows
    for key, (app, mname, M, tm) in catalogue.items():
        ids = referenced.get(tm["sql_table"] or "", set())
        if not ids:
            fields[app][mname] = {"scope": "fk_hop", "n_total": 0, "n_projected": 0,
                                  "truncated": False, "field_types": tm["field_types"],
                                  "primary_key": "id", "foreign_keys": _view_fks(tm, app), "rows": {}}
            continue
        try:
            rows = [r for r in M.all() if getattr(r, "id", None) in ids]
        except Exception:
            continue
        n_total = len(rows)
        rows = rows[:cap]
        fields[app][mname] = {"scope": "fk_hop", "n_total": n_total, "n_projected": len(rows),
                              "truncated": n_total > cap, "field_types": tm["field_types"],
                              "primary_key": "id", "foreign_keys": _view_fks(tm, app),
                              "rows": {str(r.id): _row_dict(r) for r in rows}}
    for app, tables in fields.items():
        for t, v in tables.items():
            meta["tables"][f"{app}.{t}"] = {k: v[k] for k in ("scope", "n_total", "n_projected", "truncated")}
    return fields, meta


def relevant_apps(task) -> list[str]:
    """Apps the instruction actually names.

    Every AppWorld task lists all 11 apps as `allowed_apps`, so that field does not
    narrow anything. Matching the instruction text is a legitimate narrowing signal --
    it uses only what the compiler itself is given, and never the ground truth.
    """
    text = (task.instruction or "").lower()
    named = [a for a in _apps(task) if a.replace("_", " ") in text or a in text]
    return named or _apps(task)


def prompt_state(world, state: dict, sample_rows: int = 2) -> dict:
    """What the contract compiler sees: counts everywhere, detail only where relevant.

    Counts for all ~90 tables are kept (1.9 KB) because invariants need world-wide
    coverage. Field lists and sample rows are restricted to the apps the instruction
    names, because the full version is ~75 KB and would crowd out the task itself.
    The `records` hash maps are withheld entirely: thousands of opaque hashes carry
    nothing a compiler can reason about.
    """
    mc = world.task.model_collection
    schemas: dict = {}
    samples: dict = {}
    for app in relevant_apps(world.task):
        coll = getattr(mc, app, None)
        if coll is None:
            continue
        schemas[app] = {}
        samples[app] = {}
        for mname in _model_names(coll):
            M = getattr(coll, mname)
            try:
                rows = M.all()[:sample_rows]
            except Exception:
                continue
            if rows:
                d = _dump(rows[0])
                schemas[app][mname] = sorted(d.keys())
                samples[app][mname] = [
                    {k: v for k, v in _dump(r).items() if k != "record_hash"} for r in rows
                ]
            else:
                schemas[app][mname] = []
    # `path_syntax` used to read "JSON Pointer into this object". That was false, and
    # it was the harness contradicting its own prompt: `/table_fields/spotify/Note` and
    # `/detailed_apps` are valid pointers into *this* object and invalid contract paths,
    # because a contract is evaluated against `{counts, records}`. On 2026-09-05 three of
    # three sanity contracts failed lint on exactly this, one of them emitting
    # `/detailed_apps` -- a key of this wrapper -- as a path root. The compiler prompt
    # already said the two legal roots; the payload told it otherwise, and the payload
    # is the thing it can see while writing each path.
    #
    # This is the same class of defect as the earlier `lint_state` bug: the model was
    # following instructions and the harness was wrong. `detailed_apps` is dropped rather
    # than reworded, because a bare top-level key that looks like a root is an attractor
    # regardless of what the prose says; callers derive it from `table_fields`.
    if "fields" in state:
        # Field-aware view: the compiler sees the typed schema and the projection's shape
        # (which tables are projected, how many rows, whether truncated), never the rows.
        schema = {}
        projection = {}
        relations = {}
        for app, tables in state["fields"].items():
            for t, v in tables.items():
                schema[f"{app}.{t}"] = v["field_types"]
                projection[f"{app}.{t}"] = {k: v[k] for k in ("scope", "n_projected", "truncated")}
                if v.get("foreign_keys"):
                    relations[f"{app}.{t}"] = v["foreign_keys"]
        return {"counts": state["counts"], "schema": schema, "sample_rows": samples,
                "projection": projection, "relations": relations,
                "supervisor_user_ids": state.get("fields_meta", {}).get("supervisor_user_ids", {}),
                "valid_path_roots": ["/counts/<app>/<Table>", "/records/<app>/<Table>"],
                "record_tables": sorted(schema),
                "relations_syntax":
                "`relations` lists declared foreign keys as {table: {column: target_table}}. A "
                "selector condition may use in_related / not_in_related (scalar field) or "
                "intersects_related (list field) with a sub-selection "
                "{table, field, where} in place of a literal list, only along these links.",
                "path_syntax":
                "State/delta predicates address the EVALUATION state through exactly two "
                "roots: /counts/<app>/<Table> (integer row count) and /records/<app>/<Table> "
                "(record set). Record predicates (kind 'record') address a table by name "
                "'<app>.<Table>' from `record_tables` and select rows by typed field "
                "conditions from `schema`. `sample_rows`, `projection` and "
                "`supervisor_user_ids` document the world; they are not addressable."}
    return {"counts": state["counts"], "table_fields": schemas, "sample_rows": samples,
            "valid_path_roots": ["/counts/<app>/<Table>", "/records/<app>/<Table>"],
            "path_syntax":
            "Contract paths address the EVALUATION state, not this object. That state "
            "has exactly two roots: /counts/<app>/<Table> (an integer row count) and "
            "/records/<app>/<Table> (the table's record set). `table_fields` and "
            "`sample_rows` below document the world; they are not addressable and must "
            "never appear in a path."}


def state_delta(pre: dict, post: dict) -> dict:
    """Added / removed / updated record ids per table, from the record-hash maps.

    This is the vocabulary AppWorld's own evaluators use (`models.changed_records`),
    and it is not derivable from row counts: adding one record and deleting another
    leaves every count unchanged.
    """
    out: dict = {}
    for app, tables in post.get("records", {}).items():
        for tname, rows in tables.items():
            before = (pre.get("records", {}).get(app, {}) or {}).get(tname, {}) or {}
            added = sorted(set(rows) - set(before))
            removed = sorted(set(before) - set(rows))
            updated = sorted(i for i in set(rows) & set(before) if rows[i] != before[i])
            if added or removed or updated:
                out[f"{app}.{tname}"] = {
                    "added": added, "removed": removed, "updated": updated,
                    "n_added": len(added), "n_removed": len(removed), "n_updated": len(updated),
                }
    return out


def _tracker_dict(tracker) -> dict:
    try:
        d = tracker.to_dict()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    # Strip the coloured tracebacks; keep the requirement text, which is the useful part.
    for key in ("passes", "failures"):
        for item in d.get(key, []) or []:
            if "trace" in item:
                item["trace"] = str(item["trace"])[:600]
    return d


def _safe_close(w) -> str | None:
    """Close a world, tolerating AppWorld's post-restore clock corruption.

    Verified 2026-09-05: after `load_state()`, `world.close()` raises
    ``AttributeError: '_freeze_time' object has no attribute 'fake_names'`` from
    freezegun, because restoring re-enters the time freezer and leaves its stack
    unbalanced. The world's *data* is restored correctly (see `op_replay`); only
    teardown fails. An unguarded `finally: w.close()` would replace a completed
    result with this exception, so the failure is captured and reported instead.
    """
    try:
        w.close()
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def op_survey_task(req: dict) -> dict:
    from appworld.task import Task
    t = Task.load(task_id=req["task_id"])
    gt = t.ground_truth
    return {
        "task_id": req["task_id"],
        "instruction": t.instruction,
        "allowed_apps": list(t.allowed_apps or []),
        "datetime": str(t.datetime),
        "supervisor": {k: v for k, v in dict(t.supervisor).items()
                       if k in ("first_name", "last_name", "email", "phone_number")},
        "ground_truth_answer": str(getattr(gt, "answer", ""))[:500],
        "evaluation_code_body": (getattr(gt, "evaluation_code_body", "") or "")[:20000],
        "solution_code": (getattr(gt, "solution_code", "") or "")[:20000],
    }


def _open(task_id: str, experiment: str):
    from appworld import AppWorld
    return AppWorld(task_id=task_id, experiment_name=experiment)


def op_state(req: dict) -> dict:
    w = _open(req["task_id"], req.get("experiment", "effectgate"))
    try:
        st = normalised_state(w, field_aware=bool(req.get("field_aware")))
        return {"state": st, "prompt_state": prompt_state(w, st),
                "instruction": w.task.instruction,
                "allowed_apps": list(w.task.allowed_apps or [])}
    finally:
        _safe_close(w)


#: AppWorld's `execute()` reports a failure inside the sandbox by *returning* a
#: traceback string, not by raising. Missing that would be dangerous: a solution that
#: crashed leaves pre_state == post_state, which looks exactly like a legitimate
#: "nothing needed to change" reference, and every contract requiring an effect would
#: then be blamed for the crash.
_EXEC_FAILURE_MARKERS = ("Execution failed. Traceback:", "Traceback (most recent call last)")


def _sandbox_error(output: str) -> str | None:
    if any(m in output for m in _EXEC_FAILURE_MARKERS):
        return output.strip()[-600:]
    return None


def _execute_and_capture(w, code: str, field_aware: bool = False) -> dict:
    """Run agent code, returning the official verdict and the observed state delta."""
    pre = normalised_state(w, field_aware=field_aware)
    t0 = time.monotonic()          # frozen inside AppWorld; kept only for shape
    outputs, error = [], None
    try:
        for chunk in ([code] if isinstance(code, str) else list(code)):
            text = str(w.execute(chunk))[:4000]
            outputs.append(text)
            if error is None:
                error = _sandbox_error(text)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    post = normalised_state(w, field_aware=field_aware)
    tracker = _tracker_dict(w.evaluate(suppress_errors=True))
    return {
        "pre_state": pre,
        "post_state": post,
        "delta": state_delta(pre, post),
        "outputs": outputs,
        "execution_error": error,
        "task_completed": bool(w.task_completed()),
        "official_evaluation": tracker,
        "inner_elapsed_s": time.monotonic() - t0,
    }


def _ground_truth_solution(task_id: str) -> str:
    """AppWorld's shipped ground-truth solution for a task.

    `Task.load(...).ground_truth.compiled_solution_code` comes back empty (the
    attribute is only populated in some load modes), so the file AppWorld ships is
    read directly from the data directory. This reads AppWorld's data; it does not
    modify anything.
    """
    import os
    from pathlib import Path
    roots = []
    try:
        from appworld import path_store
        for attr in ("data", "root", "base"):
            v = getattr(path_store, attr, None)
            if v:
                roots.append(Path(str(v)))
    except Exception:
        pass
    roots += [Path(os.getenv("APPWORLD_ROOT", ".")), Path.cwd()]
    for root in roots:
        for candidate in (root / "data" / "tasks" / task_id / "ground_truth",
                          root / "tasks" / task_id / "ground_truth"):
            f = candidate / "compiled_solution.py"
            if f.is_file():
                return f.read_text()
    return ""


def op_reference_run(req: dict) -> dict:
    """Execute AppWorld's own ground-truth solution: the known-good behaviour."""
    code = _ground_truth_solution(req["task_id"])
    if not code:
        return {"error": "no ground-truth solution file found for this task"}
    # The file defines `def solution(apis, requester)`; both names are in the
    # execute namespace, so it is defined and then invoked.
    code = code + "\n\nsolution(apis, requester)\n"
    w = _open(req["task_id"], req.get("experiment", "effectgate_reference"))
    try:
        out = _execute_and_capture(w, code, field_aware=bool(req.get("field_aware")))
        out["solution_code_lines"] = code.count("\n") + 1
        return out
    finally:
        _safe_close(w)


def op_run_code(req: dict) -> dict:
    w = _open(req["task_id"], req.get("experiment", "effectgate_candidate"))
    try:
        return _execute_and_capture(w, req["code"], field_aware=bool(req.get("field_aware")))
    finally:
        _safe_close(w)


#: Snapshot label used by the replay loop. AppWorld's `save_state` takes a caller-chosen
#: id and returns it; restoring is `load_state(<that id>)`.
REPLAY_SNAPSHOT_ID = "effectgate_pre"


def op_replay(req: dict) -> dict:
    """Snapshot once, then run each candidate from the **restored** pre-state.

    This is `STATEFUL_REPLAY_SPEC`'s gate loop for AppWorld, using the benchmark's own
    save/load rather than a snapshot mechanism of our own (the spec asks for exactly
    that where an official one exists).

    Verified 2026-09-05 on `b7a9ee9_1`: after running the ground-truth solution
    (+5 `spotify.UserArtistFollowing` rows) and calling `load_state`, both the count
    view and the record-hash view are **byte-identical** to the pre-state, the residual
    delta is empty, and a re-run reproduces the same delta and the same official
    verdict. So candidates are comparable across a single world instance, which also
    avoids paying AppWorld's ~19 s initialisation per candidate.

    `candidates` is a list of ``{"name": str, "code": str}``. ``"reference"`` as a code
    value is replaced by AppWorld's own shipped ground-truth solution.
    """
    task_id = req["task_id"]
    candidates = req.get("candidates") or []
    w = _open(task_id, req.get("experiment", "effectgate_replay"))
    results: list[dict] = []
    fa = bool(req.get("field_aware"))
    try:
        pre = normalised_state(w, field_aware=fa)
        snap = w.save_state(REPLAY_SNAPSHOT_ID)
        for cand in candidates:
            restore_ok, restore_error = True, None
            if results:                      # the first candidate already starts at pre
                try:
                    w.load_state(snap)
                except Exception as exc:
                    restore_ok, restore_error = False, f"{type(exc).__name__}: {exc}"
                restored = normalised_state(w, field_aware=fa)
                # Restoration fidelity is *measured every time*, not assumed. A gate
                # that silently ran a candidate from a dirty state would attribute the
                # previous candidate's effects to this one.
                restore_exact = (restored["counts"] == pre["counts"]
                                 and restored["records"] == pre["records"]
                                 and restored.get("fields") == pre.get("fields"))
                residual = state_delta(pre, restored)
            else:
                restore_exact, residual = True, {}
            out = _execute_and_capture(w, cand["code"] if cand.get("code") != "reference"
                                       else _reference_code(task_id), field_aware=fa)
            out.update({"candidate": cand.get("name", f"c{len(results)}"),
                        "restore_ok": restore_ok, "restore_error": restore_error,
                        "restore_exact": restore_exact,
                        "residual_delta_after_restore": {
                            k: {kk: vv for kk, vv in v.items() if kk.startswith("n_")}
                            for k, v in residual.items()}})
            # The pre-state is the shared snapshot, so it is reported once, not per run.
            out.pop("pre_state", None)
            results.append(out)
    finally:
        close_error = _safe_close(w)
    return {"task_id": task_id, "pre_state": pre, "snapshot_id": snap,
            "candidates": results, "close_error": close_error}


def _reference_code(task_id: str) -> str:
    code = _ground_truth_solution(task_id)
    if not code:
        raise RuntimeError(f"no ground-truth solution file for {task_id}")
    return code + "\n\nsolution(apis, requester)\n"


#: Execution *variants* of the reference solution, for externally-labelled validation of
#: an oracle. All are generic: they wrap every mutating API (method != GET, read from
#: AppWorld's own api docs) of the task's apps and alter the k-th mutating call. The
#: official evaluator then labels each variant; the oracle under test is scored against
#: that label. Nothing task-specific.
_VARIANT_PRELUDE = """
import json as _json
_mutating = set()
for _app in %(apps)r:
    try:
        for _d in apis.api_docs.show_api_descriptions(app_name=_app):
            _doc = apis.api_docs.show_api_doc(app_name=_app, api_name=_d['name'])
            # session endpoints are POSTs that carry no task effect; skipping them only
            # crashes the solution and yields an uninformative mutant
            if str(_doc.get('method', 'GET')).upper() != 'GET' and not any(
                    _s in _d['name'] for _s in ('login', 'logout', 'signup', 'password')):
                _mutating.add((_app, _d['name']))
    except Exception:
        pass
_calls = [0]
_MODE = %(mode)r
_K = %(k)d
class _Wrapped:
    def __init__(self, app, name, fn):
        self._app, self._name, self._fn = app, name, fn
    def __call__(self, *a, **kw):
        _calls[0] += 1
        n = _calls[0]
        if _MODE == 'skip_k' and n == _K:
            return {'message': 'skipped by variant harness'}
        if _MODE == 'skip_all':
            return {'message': 'skipped by variant harness'}
        r = self._fn(*a, **kw)
        if _MODE == 'dup_k' and n == _K:
            try:
                self._fn(*a, **kw)
            except Exception:
                pass
        return r
for (_app, _name) in _mutating:
    try:
        _obj = getattr(apis, _app)
        setattr(_obj, _name, _Wrapped(_app, _name, getattr(_obj, _name)))
    except Exception:
        pass
"""


def op_variants(req: dict) -> dict:
    """Run the reference and generic execution variants from a restored snapshot.

    Variants (all official-evaluator labelled):
      reference          the shipped ground-truth solution
      skip_k (k=1..K)    the k-th mutating API call is not performed   (missing effect)
      dup_k (k=last)     the k-th mutating API call is performed twice (duplicate effect)
      skip_all           no mutating call is performed                 (no-op)
      extra_read         the reference plus a harmless read call       (valid alternative)
      foreign            another task's ground-truth solution          (wrong effect)
    """
    task_id = req["task_id"]
    apps = [a for a in req.get("apps") or [] if a not in ("api_docs", "supervisor")]
    ref_code = _reference_code(task_id)
    n_skip = int(req.get("n_skip", 2))
    cands = [{"name": "reference", "code": ref_code}]
    for k in range(1, n_skip + 1):
        cands.append({"name": f"skip_{k}", "code": _VARIANT_PRELUDE % {"apps": apps, "mode": "skip_k", "k": k} + ref_code})
    cands.append({"name": "dup_1", "code": _VARIANT_PRELUDE % {"apps": apps, "mode": "dup_k", "k": 1} + ref_code})
    cands.append({"name": "skip_all", "code": _VARIANT_PRELUDE % {"apps": apps, "mode": "skip_all", "k": 0} + ref_code})
    cands.append({"name": "extra_read", "code": ref_code + "\n\ntry:\n    apis.api_docs.show_app_descriptions()\nexcept Exception:\n    pass\n"})
    if req.get("foreign_task_id"):
        try:
            cands.append({"name": "foreign", "code": _reference_code(req["foreign_task_id"])})
        except Exception:
            pass
    return op_replay({"task_id": task_id, "candidates": cands, "field_aware": bool(req.get("field_aware", True)),
                      "experiment": req.get("experiment", "effectgate_variants")})


def op_evaluate_task(req: dict) -> dict:
    """AppWorld's out-of-process official evaluator.

    `evaluate_task(task_id, experiment_name)` replays the experiment's saved logs and
    re-runs the same ground-truth evaluation code as the in-process `world.evaluate()`.
    Keeping both lets the sanity run cross-check that our in-process verdict is the one
    AppWorld would report on its own, rather than assuming it.
    """
    from appworld import evaluate_task
    tracker = evaluate_task(task_id=req["task_id"],
                            experiment_name=req.get("experiment", "effectgate_reference"),
                            suppress_errors=True, save_report=False)
    return {"task_id": req["task_id"], "official_evaluation": _tracker_dict(tracker)}


def op_api_check(req: dict) -> dict:
    """Record the installed AppWorld API surface, so verification is an artifact.

    MASTER_PROMPT requires the AppWorld API to be verified against the installation
    rather than assumed. Running that as a worker op means it is reproducible and
    re-checkable after any AppWorld upgrade, instead of a one-off console session.
    """
    import importlib.metadata as md
    import inspect

    from appworld import AppWorld, evaluate_task, load_task_ids
    from appworld.task import Task

    splits = {}
    for name in ("train", "dev", "test_normal", "test_challenge"):
        try:
            splits[name] = len(load_task_ids(name))
        except Exception as exc:
            splits[name] = f"error: {type(exc).__name__}: {exc}"
    methods = sorted(n for n, v in inspect.getmembers(AppWorld, callable)
                     if not n.startswith("_"))
    return {
        "appworld_version": md.version("appworld"),
        "pydantic_version": __import__("pydantic").VERSION,
        "splits": splits,
        "appworld_public_methods": methods,
        "has_save_state": "save_state" in methods,
        "has_load_state": "load_state" in methods,
        "has_evaluate": "evaluate" in methods,
        "signatures": {
            "load_task_ids": str(inspect.signature(load_task_ids)),
            "evaluate_task": str(inspect.signature(evaluate_task)),
            "AppWorld.save_state": str(inspect.signature(AppWorld.save_state)),
            "AppWorld.load_state": str(inspect.signature(AppWorld.load_state)),
            "AppWorld.evaluate": str(inspect.signature(AppWorld.evaluate)),
            "AppWorld.execute": str(inspect.signature(AppWorld.execute)),
        },
        "task_ground_truth_attrs": sorted(
            a for a in dir(Task.load(task_id=load_task_ids("train")[0]).ground_truth)
            if not a.startswith("_")),
    }


OPS = {
    "survey_task": op_survey_task,
    "state": op_state,
    "reference_run": op_reference_run,
    "run_code": op_run_code,
    "replay": op_replay,
    "variants": op_variants,
    "evaluate_task": op_evaluate_task,
    "api_check": op_api_check,
}


def main() -> int:
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    try:
        req = json.loads(raw)
        fn = OPS.get(req.get("op"))
        if fn is None:
            result = {"ok": False, "error": f"unknown op {req.get('op')!r}",
                      "known_ops": sorted(OPS)}
        else:
            result = {"ok": True, "result": fn(req)}
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc()[-3000:]}
    # A single marker line keeps the payload separable from AppWorld's own chatter.
    sys.stdout.write("\n__EFFECTGATE_JSON__" + json.dumps(result, default=str) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
