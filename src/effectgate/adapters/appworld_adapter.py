"""AppWorld adapter — Phase B.

AppWorld runs in its own virtualenv (it pins pydantic 1.x), so this adapter drives it
through `scripts/appworld_worker.py` in a subprocess and exchanges JSON. Nothing in
AppWorld is modified; its official evaluator is called as-is and treated as gold.

Every API used here was verified against the installed package on 2026-08-13 --
see `notes/APPWORLD_API_NOTES.md`. Nothing is assumed from memory.

State view (built by the worker, justified there):

``counts``   every table in the task's allowed apps -> row count. Complete collateral
             coverage for ~120 integers, against a world of ~365k rows.
``records``  ``{table: {row_id: record_hash}}`` for record-level change detection.

Latency is measured **here**, in the calling process: AppWorld freezes its own clock,
so timing taken inside it is meaningless.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKER = REPO_ROOT / "scripts" / "appworld_worker.py"
MARKER = "__EFFECTGATE_JSON__"

DEFAULT_VENV = REPO_ROOT / ".venv-appworld"
DEFAULT_ROOT = Path(os.getenv("APPWORLD_ROOT", str(Path.home() / "appworld-root")))


class AppWorldError(RuntimeError):
    """The AppWorld subprocess failed. Distinct from a task or contract failure."""


@dataclass
class AppWorldRun:
    """One execution of agent code against a task, with the official verdict."""
    task_id: str
    pre_state: dict[str, Any]
    post_state: dict[str, Any]
    delta: dict[str, Any]
    outputs: list[str]
    execution_error: str | None
    task_completed: bool
    official_evaluation: dict[str, Any]
    wall_time_s: float = 0.0
    solution_code_lines: int | None = None

    @property
    def official_success(self) -> bool | None:
        return self.official_evaluation.get("success")

    @property
    def changed_tables(self) -> list[str]:
        return sorted(self.delta)

    def official_requirements(self) -> list[dict[str, Any]]:
        """Per-requirement verdicts, which is what a contract can be compared against.

        `success` alone conflates answer-correctness with state-correctness; only the
        state requirements are in an effect contract's scope.
        """
        out = []
        for item in self.official_evaluation.get("passes", []) or []:
            out.append({"requirement": item.get("requirement", ""), "passed": True})
        for item in self.official_evaluation.get("failures", []) or []:
            out.append({"requirement": item.get("requirement", ""), "passed": False,
                        "trace": item.get("trace", "")})
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "delta": self.delta,
            "changed_tables": self.changed_tables,
            "outputs": [o[:500] for o in self.outputs],
            "execution_error": self.execution_error,
            "task_completed": self.task_completed,
            "official_success": self.official_success,
            "official_evaluation": self.official_evaluation,
            "wall_time_s": self.wall_time_s,
            "solution_code_lines": self.solution_code_lines,
        }


@dataclass
class AppWorldAdapter:
    """Drives AppWorld from the EffectGate virtualenv."""

    name: str = "appworld"
    venv: Path = field(default_factory=lambda: DEFAULT_VENV)
    root: Path = field(default_factory=lambda: DEFAULT_ROOT)
    timeout_s: int = 900

    # ------------------------------------------------------------------ plumbing
    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    def available(self) -> bool:
        return self.python.exists() and (self.root / "data").is_dir()

    def _call(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.python.exists():
            raise AppWorldError(
                f"AppWorld virtualenv missing at {self.venv}. Run scripts/install_appworld.sh")
        if not (self.root / "data").is_dir():
            raise AppWorldError(
                f"AppWorld data missing at {self.root}/data. Run scripts/install_appworld.sh")
        t0 = time.perf_counter()
        proc = subprocess.run(
            [str(self.python), str(WORKER), json.dumps(request)],
            cwd=str(self.root), capture_output=True, text=True, timeout=self.timeout_s,
        )
        wall = time.perf_counter() - t0
        payload = None
        for line in proc.stdout.splitlines():
            if line.startswith(MARKER):
                payload = json.loads(line[len(MARKER):])
        if payload is None:
            raise AppWorldError(
                f"worker produced no result for op={request.get('op')!r} "
                f"(rc={proc.returncode}); stderr tail: {proc.stderr[-800:]}")
        if not payload.get("ok"):
            raise AppWorldError(
                f"worker error for op={request.get('op')!r}: {payload.get('error')}\n"
                f"{payload.get('traceback', '')[-1200:]}")
        result = payload["result"]
        result["_wall_time_s"] = wall
        return result

    # -------------------------------------------------------------------- public
    def survey_task(self, task_id: str) -> dict[str, Any]:
        """Instruction, allowed apps, ground-truth answer and the official evaluator body."""
        return self._call({"op": "survey_task", "task_id": task_id})

    def get_state(self, task_id: str, field_aware: bool = False) -> dict[str, Any]:
        """Freshly initialised state for a task, plus the compiler-facing view.

        ``field_aware=True`` adds the typed, owner-scoped field projection (DSL v0.4);
        the default is the hash-only view, byte-identical to the frozen baseline.
        """
        return self._call({"op": "state", "task_id": task_id, "field_aware": field_aware})

    def reference_run(self, task_id: str, field_aware: bool = False) -> AppWorldRun:
        """Execute AppWorld's own ground-truth solution.

        This is the *known-good* behaviour: the reference agent version whose final
        state self-validation is allowed to see. It is AppWorld's code, not ours.
        """
        r = self._call({"op": "reference_run", "task_id": task_id, "field_aware": field_aware})
        if "error" in r:
            raise AppWorldError(f"{task_id}: {r['error']}")
        return self._to_run(task_id, r)

    def verify_api(self) -> dict[str, Any]:
        """The installed AppWorld API surface, read from the installation.

        MASTER_PROMPT forbids assuming the API from memory. This makes the check a
        reproducible artifact rather than a console session, so it can be re-run after
        any AppWorld upgrade.
        """
        return self._call({"op": "api_check"})

    def evaluate_task(self, task_id: str,
                      experiment: str = "effectgate_reference") -> dict[str, Any]:
        """AppWorld's out-of-process official evaluator, for cross-checking.

        `world.evaluate()` (used during a run) and `evaluate_task()` (used afterwards,
        from the saved experiment logs) run the same unmodified ground-truth code. The
        sanity run compares them so "we called the official evaluator" is verified
        rather than claimed.
        """
        return self._call({"op": "evaluate_task", "task_id": task_id,
                           "experiment": experiment})

    def replay(self, task_id: str,
               candidates: list[dict[str, str]], field_aware: bool = False) -> dict[str, Any]:
        """Snapshot the task once, then run each candidate from the restored pre-state.

        `candidates` is ``[{"name": ..., "code": ...}]``; the code value ``"reference"``
        runs AppWorld's own shipped ground-truth solution.

        Uses AppWorld's native `save_state`/`load_state` rather than a snapshot
        mechanism of our own, as STATEFUL_REPLAY_SPEC directs where an official one
        exists. Each restored run reports `restore_exact` and any
        `residual_delta_after_restore`, so a dirty restore is visible in the record
        instead of being silently attributed to the candidate.
        """
        return self._call({"op": "replay", "task_id": task_id, "candidates": candidates,
                           "field_aware": field_aware})

    def variants(self, task_id: str, apps: list[str], n_skip: int = 2,
                 foreign_task_id: str | None = None, field_aware: bool = True) -> dict[str, Any]:
        """Reference + generic execution variants (skip/dup/no-op/extra-read/foreign), each
        with the official evaluator's verdict, from one restored world. See the worker."""
        return self._call({"op": "variants", "task_id": task_id, "apps": apps, "n_skip": n_skip,
                           "foreign_task_id": foreign_task_id, "field_aware": field_aware})

    def run_code(self, task_id: str, code: str, field_aware: bool = False) -> AppWorldRun:
        """Execute candidate agent code against a freshly initialised task."""
        return self._to_run(task_id, self._call(
            {"op": "run_code", "task_id": task_id, "code": code, "field_aware": field_aware}))

    @staticmethod
    def _to_run(task_id: str, r: dict[str, Any]) -> AppWorldRun:
        return AppWorldRun(
            task_id=task_id,
            pre_state=r["pre_state"], post_state=r["post_state"], delta=r["delta"],
            outputs=r.get("outputs", []), execution_error=r.get("execution_error"),
            task_completed=bool(r.get("task_completed")),
            official_evaluation=r.get("official_evaluation", {}),
            wall_time_s=r.get("_wall_time_s", 0.0),
            solution_code_lines=r.get("solution_code_lines"),
        )


def load_survey(path: str | Path | None = None) -> list[dict[str, Any]]:
    """The task survey produced by `scripts/appworld_survey.py`."""
    p = Path(path) if path else REPO_ROOT / "results" / "appworld" / "survey.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def side_effecting_tasks(
    survey: list[dict[str, Any]] | None = None, split: str | None = None
) -> list[dict[str, Any]]:
    """Tasks whose official evaluator expects state to change.

    Read-only tasks are excluded on principle, not for convenience: an effect
    contract judges a state transition and has no content for "answer this question
    and change nothing" (PROJECT_CHARTER.md scopes EffectGate to side-effecting agents).
    """
    rows = survey if survey is not None else load_survey()
    out = [r for r in rows if r.get("kind") == "side_effecting"]
    if split:
        out = [r for r in out if r.get("split") == split]
    return out


def simplest_tasks(n: int = 5, split: str | None = None,
                   diversify: bool = True) -> list[dict[str, Any]]:
    """The least complex side-effecting tasks, spread across scenarios and apps.

    AppWorld task ids are ``<scenario>_<variant>``: `b7a9ee9_1`, `b7a9ee9_2` and
    `b7a9ee9_3` are the same task template with a different genre. Taking the top-N by
    complexity alone returns three paraphrases of one task, which tells you nothing
    about generality. With ``diversify`` we take at most one variant per scenario, and
    prefer unseen target tables, before falling back to fill the quota.
    """
    rows = sorted(side_effecting_tasks(split=split),
                  key=lambda r: (r.get("n_expected_changed") or 99,
                                 r.get("n_eval_chars") or 0))
    if not diversify:
        return rows[:n]

    picked: list[dict[str, Any]] = []
    seen_scenarios: set[str] = set()
    seen_tables: set[str] = set()
    for r in rows:
        if len(picked) >= n:
            break
        scenario = str(r["task_id"]).rsplit("_", 1)[0]
        if scenario in seen_scenarios:
            continue
        tables = set(r.get("expected_changed_models") or r.get("changed_records_models") or [])
        if tables and tables <= seen_tables:
            continue          # a table family we already cover; keep looking
        picked.append(r)
        seen_scenarios.add(scenario)
        seen_tables |= tables
    # Fall back to distinct scenarios only, then to anything, to honour the quota.
    for r in rows:
        if len(picked) >= n:
            break
        if str(r["task_id"]).rsplit("_", 1)[0] in seen_scenarios:
            continue
        picked.append(r)
        seen_scenarios.add(str(r["task_id"]).rsplit("_", 1)[0])
    return picked[:n]
