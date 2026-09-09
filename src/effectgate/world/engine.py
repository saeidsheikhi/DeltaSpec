"""Deterministic tool interpreter over a normalized JSON world state.

The world has five state roots:

``files``    ``{dir: {filename: content}}``
``messages`` ``{user: [text, ...]}``
``tickets``  ``{ticket_id: {title, status, assignee, priority}}``
``config``   ``{key: value}``
``db``       ``{table: {row_id: {field: value}}}``

Tools are total functions on the state: a failing tool call raises no exception and
mutates nothing, it just records an unsuccessful :class:`ToolEvent`. This keeps
snapshot/restore exact and makes every run reproducible.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from effectgate.models import ToolCall, ToolEvent

STATE_ROOTS = ("files", "messages", "tickets", "config", "db")


def empty_state() -> dict[str, Any]:
    return {"files": {}, "messages": {}, "tickets": {}, "config": {}, "db": {}}


class ToolFailure(Exception):
    """Raised inside a tool implementation to reject a call without mutating state."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    args: dict[str, str]
    description: str
    mutating: bool
    fn: Callable[[dict[str, Any], dict[str, Any]], Any]

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "args": dict(self.args),
            "description": self.description,
            "mutating": self.mutating,
        }


# --------------------------------------------------------------------- tools
def _dir(state: dict, d: str) -> dict:
    files = state["files"]
    if d not in files:
        raise ToolFailure(f"no such directory: {d}")
    return files[d]


def _t_list_dir(state, a):
    return sorted(_dir(state, a["dir"]).keys())


def _t_read_file(state, a):
    d = _dir(state, a["dir"])
    if a["name"] not in d:
        raise ToolFailure(f"no such file: {a['dir']}/{a['name']}")
    return d[a["name"]]


def _t_move_file(state, a):
    src, dst, name = _dir(state, a["src_dir"]), _dir(state, a["dst_dir"]), a["name"]
    if name not in src:
        raise ToolFailure(f"no such file: {a['src_dir']}/{name}")
    dst[name] = src.pop(name)
    return {"moved": name}


def _t_copy_file(state, a):
    src, dst, name = _dir(state, a["src_dir"]), _dir(state, a["dst_dir"]), a["name"]
    if name not in src:
        raise ToolFailure(f"no such file: {a['src_dir']}/{name}")
    dst[name] = src[name]
    return {"copied": name}


def _t_delete_file(state, a):
    d, name = _dir(state, a["dir"]), a["name"]
    if name not in d:
        raise ToolFailure(f"no such file: {a['dir']}/{name}")
    d.pop(name)
    return {"deleted": name}


def _t_write_file(state, a):
    d = _dir(state, a["dir"])
    d[a["name"]] = str(a["content"])
    return {"written": a["name"]}


def _t_send_message(state, a):
    state["messages"].setdefault(a["recipient"], []).append(str(a["text"]))
    return {"sent_to": a["recipient"]}


def _t_create_ticket(state, a):
    tid = a["ticket_id"]
    if tid in state["tickets"]:
        raise ToolFailure(f"ticket already exists: {tid}")
    state["tickets"][tid] = {
        "title": str(a.get("title", "")),
        "status": str(a.get("status", "open")),
        "assignee": str(a.get("assignee", "unassigned")),
        "priority": str(a.get("priority", "normal")),
    }
    return {"created": tid}


def _t_set_ticket_field(state, a):
    tid, fieldname = a["ticket_id"], a["field"]
    if tid not in state["tickets"]:
        raise ToolFailure(f"no such ticket: {tid}")
    if fieldname not in ("title", "status", "assignee", "priority"):
        raise ToolFailure(f"unknown ticket field: {fieldname}")
    state["tickets"][tid][fieldname] = a["value"]
    return {"updated": tid}


def _t_delete_ticket(state, a):
    tid = a["ticket_id"]
    if tid not in state["tickets"]:
        raise ToolFailure(f"no such ticket: {tid}")
    state["tickets"].pop(tid)
    return {"deleted": tid}


def _t_set_config(state, a):
    state["config"][a["key"]] = a["value"]
    return {"set": a["key"]}


def _t_db_set(state, a):
    table = state["db"].setdefault(a["table"], {})
    row = table.setdefault(a["row_id"], {})
    row[a["field"]] = a["value"]
    return {"row": a["row_id"]}


def _t_db_delete(state, a):
    table = state["db"].get(a["table"])
    if not table or a["row_id"] not in table:
        raise ToolFailure(f"no such row: {a['table']}/{a['row_id']}")
    table.pop(a["row_id"])
    return {"deleted": a["row_id"]}


TOOLS: dict[str, ToolSpec] = {
    t.name: t for t in [
        ToolSpec("list_dir", {"dir": "string"}, "List filenames in a directory.", False, _t_list_dir),
        ToolSpec("read_file", {"dir": "string", "name": "string"}, "Read a file's content.", False, _t_read_file),
        ToolSpec("move_file", {"src_dir": "string", "dst_dir": "string", "name": "string"},
                 "Move a file between directories.", True, _t_move_file),
        ToolSpec("copy_file", {"src_dir": "string", "dst_dir": "string", "name": "string"},
                 "Copy a file, leaving the original in place.", True, _t_copy_file),
        ToolSpec("delete_file", {"dir": "string", "name": "string"},
                 "Permanently delete a file.", True, _t_delete_file),
        ToolSpec("write_file", {"dir": "string", "name": "string", "content": "string"},
                 "Create or overwrite a file.", True, _t_write_file),
        ToolSpec("send_message", {"recipient": "string", "text": "string"},
                 "Append a message to a user's inbox.", True, _t_send_message),
        ToolSpec("create_ticket",
                 {"ticket_id": "string", "title": "string", "status": "string",
                  "assignee": "string", "priority": "string"},
                 "Create a new ticket.", True, _t_create_ticket),
        ToolSpec("set_ticket_field", {"ticket_id": "string", "field": "string", "value": "string"},
                 "Set one field of an existing ticket (title|status|assignee|priority).", True,
                 _t_set_ticket_field),
        ToolSpec("delete_ticket", {"ticket_id": "string"}, "Delete a ticket.", True, _t_delete_ticket),
        ToolSpec("set_config", {"key": "string", "value": "any"},
                 "Set one configuration key.", True, _t_set_config),
        ToolSpec("db_set", {"table": "string", "row_id": "string", "field": "string", "value": "any"},
                 "Create or update one field of one database row.", True, _t_db_set),
        ToolSpec("db_delete", {"table": "string", "row_id": "string"},
                 "Delete a database row.", True, _t_db_delete),
    ]
}


@dataclass
class RunOutcome:
    final_state: dict[str, Any]
    trace: list[ToolEvent]
    n_failed_calls: int
    final_answer: str = ""

    def tool_call_signature(self, normalized: bool = True) -> list[str]:
        """Trace representation used by the tool-trace baselines."""
        if normalized:
            return sorted(f"{e.name}({_arg_sig(e.args)})" for e in self.trace if e.ok)
        return [f"{e.name}({_arg_sig(e.args)})" for e in self.trace]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace": [e.to_dict() for e in self.trace],
            "n_failed_calls": self.n_failed_calls,
            "final_answer": self.final_answer,
        }


def _arg_sig(args: dict[str, Any]) -> str:
    return ",".join(f"{k}={args[k]!r}" for k in sorted(args))


class ToyWorldEngine:
    """A mutable world plus an exact snapshot/restore facility."""

    def __init__(self, state: dict[str, Any] | None = None):
        self.state: dict[str, Any] = copy.deepcopy(state) if state else empty_state()
        self.trace: list[ToolEvent] = []

    # ---------------------------------------------------------------- state
    def get_state(self) -> dict[str, Any]:
        return copy.deepcopy(self.state)

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.state)

    def restore(self, handle: dict[str, Any]) -> None:
        self.state = copy.deepcopy(handle)
        self.trace = []

    # ---------------------------------------------------------------- tools
    def apply(self, call: ToolCall, allowed: set[str] | None = None) -> ToolEvent:
        idx = len(self.trace)
        spec = TOOLS.get(call.name)
        if spec is None:
            ev = ToolEvent(idx, call.name, dict(call.args), False, None, f"unknown tool: {call.name}")
            self.trace.append(ev)
            return ev
        if allowed is not None and call.name not in allowed:
            ev = ToolEvent(idx, call.name, dict(call.args), False, None,
                           f"tool not available for this task: {call.name}")
            self.trace.append(ev)
            return ev
        missing = [a for a in spec.args if a not in call.args]
        if missing:
            ev = ToolEvent(idx, call.name, dict(call.args), False, None,
                           f"missing required args: {missing}")
            self.trace.append(ev)
            return ev

        # Tools mutate a working copy so a failure leaves the world untouched.
        working = copy.deepcopy(self.state)
        try:
            result = spec.fn(working, dict(call.args))
        except ToolFailure as exc:
            ev = ToolEvent(idx, call.name, dict(call.args), False, None, str(exc))
            self.trace.append(ev)
            return ev
        except Exception as exc:  # defensive: never let a tool crash a whole run
            ev = ToolEvent(idx, call.name, dict(call.args), False, None,
                           f"{type(exc).__name__}: {exc}")
            self.trace.append(ev)
            return ev
        self.state = working
        ev = ToolEvent(idx, call.name, dict(call.args), True, result, None)
        self.trace.append(ev)
        return ev

    def run_plan(self, plan: list[ToolCall], allowed: set[str] | None = None) -> RunOutcome:
        for call in plan:
            self.apply(call, allowed)
        failed = sum(1 for e in self.trace if not e.ok)
        return RunOutcome(self.get_state(), list(self.trace), failed)


def tool_schemas(names: list[str]) -> list[dict[str, Any]]:
    return [TOOLS[n].schema() for n in names if n in TOOLS]


def state_diff(s0: dict[str, Any], s1: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat list of ``{path, before, after}`` for every leaf that differs."""
    out: list[dict[str, Any]] = []

    def walk(a: Any, b: Any, path: str) -> None:
        # An added or removed object is decomposed into its leaves so that the effect
        # footprint always names concrete leaf paths.
        if isinstance(a, dict) or isinstance(b, dict):
            if isinstance(a, dict) or a is _ABSENT:
                if isinstance(b, dict) or b is _ABSENT:
                    da = a if isinstance(a, dict) else {}
                    db = b if isinstance(b, dict) else {}
                    for k in sorted(set(da) | set(db)):
                        walk(da.get(k, _ABSENT), db.get(k, _ABSENT), f"{path}/{_esc(k)}")
                    return
        if a != b:
            out.append({
                "path": path,
                "before": None if a is _ABSENT else a,
                "after": None if b is _ABSENT else b,
                "kind": "added" if a is _ABSENT else "removed" if b is _ABSENT else "modified",
            })

    walk(s0, s1, "")
    return out


class _Absent:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "<ABSENT>"


_ABSENT = _Absent()


def _esc(k: str) -> str:
    return str(k).replace("~", "~0").replace("/", "~1")


@dataclass
class EffectFootprint:
    """Which parts of the state a correct execution of a task is expected to touch."""
    changed_paths: list[str] = field(default_factory=list)
    touched_roots: list[str] = field(default_factory=list)

    @classmethod
    def from_diff(cls, s0: dict, s1: dict) -> "EffectFootprint":
        d = state_diff(s0, s1)
        paths = [x["path"] for x in d]
        roots = sorted({p.split("/")[1] for p in paths if len(p.split("/")) > 1})
        return cls(paths, roots)
