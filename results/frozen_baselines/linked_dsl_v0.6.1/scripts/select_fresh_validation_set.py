#!/usr/bin/env python3
"""Select a small fresh AppWorld validation set NOT used to design the field-aware system.

Rules, all mechanical and recorded in the output:

* train/dev only (never test_normal / test_challenge);
* side-effecting tasks with a valid reference (ground truth passes AppWorld's evaluator,
  per results/appworld/reference_validity_summary.json where known);
* **exclude every scenario** (task-id prefix) that appears in the diagnostic set or in
  any earlier Phase B pilot -- those tasks exposed the failure and shaped the design;
* at most one task per scenario, and at most two per app, so the set is not dominated
  by the apps the v4 prompt's worked examples use (todoist, phone, file_system) nor by
  spotify, which dominates the diagnostic set;
* deterministic: seeded shuffle within the eligible pool.

    python scripts/select_fresh_validation_set.py --n 8 --seed 42
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from effectgate.adapters.appworld_adapter import load_survey, side_effecting_tasks  # noqa: E402

FROZEN = REPO / "results" / "frozen_baselines" / "hash_only_dsl_v0.3" / "results"
PRIOR_RESULT_FILES = [
    FROZEN / "appworld_pilot" / "sanity_runs.jsonl",
    FROZEN / "appworld_phaseB" / "sanity_runs.jsonl",
    REPO / "results" / "appworld_v3" / "sanity_runs.jsonl",
    REPO / "results" / "appworld" / "sanity_runs.jsonl",
    REPO / "results" / "appworld_fieldaware" / "sanity_runs.jsonl",
    REPO / "results" / "appworld_relational" / "sanity_runs.jsonl",
    REPO / "results" / "appworld_relational_probe" / "sanity_runs.jsonl",
    REPO / "results" / "appworld_linked" / "sanity_runs.jsonl",
    REPO / "results" / "frozen_baselines" / "relational_dsl_v0.5" / "results" / "appworld_relational" / "sanity_runs.jsonl",
]
#: Every task id that shaped any design decision, regardless of whether a result file
#: still lists it. Belt and braces: the seven diagnostic tasks are excluded by name too.
DESIGN_TASK_IDS = {"b7a9ee9_1", "b0a8eae_1", "3ab5b8b_1", "cf6abd2_1", "4fab96f_1",
                   "ccb4494_1", "692c77d_1", "229360a_1"}
EXAMPLE_APPS = {"todoist", "phone", "file_system"}      # used in the v4 prompt's examples


def used_task_ids() -> set[str]:
    out: set[str] = set()
    for f in PRIOR_RESULT_FILES:
        if f.exists():
            for line in f.read_text().splitlines():
                if line.strip():
                    out.add(json.loads(line)["task_id"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--per-app", type=int, default=2,
                    help="max tasks per primary app. 2 was the pre-registered value; with every "
                         "prior scenario excluded it yields only 6 tasks, so the recorded run "
                         "for v0.6.1 uses 4 (see DECISIONS, 2026-09-05)")
    ap.add_argument("--out", default=str(REPO / "results" / "appworld_linked" / "fresh_set_selection.json"))
    args = ap.parse_args()

    used = used_task_ids() | DESIGN_TASK_IDS
    used_scenarios = {t.rsplit("_", 1)[0] for t in used}
    invalid = set()
    vpath = REPO / "results" / "appworld" / "reference_validity_summary.json"
    if vpath.exists():
        invalid = set(json.loads(vpath.read_text()).get("invalid_task_ids") or [])

    pool = [r for r in side_effecting_tasks()
            if r.get("split") in ("train", "dev")
            and r["task_id"] not in used and r["task_id"] not in invalid
            and r["task_id"].rsplit("_", 1)[0] not in used_scenarios]
    rng = random.Random(args.seed)
    rng.shuffle(pool)

    def primary_app(r) -> str:
        models = r.get("expected_changed_models") or []
        return models[0].split(".")[0] if models else "unknown"

    chosen, seen_scen, per_app = [], set(), {}
    for r in pool:
        scen = r["task_id"].rsplit("_", 1)[0]
        app = primary_app(r)
        if scen in seen_scen or per_app.get(app, 0) >= args.per_app:
            continue
        chosen.append(r); seen_scen.add(scen); per_app[app] = per_app.get(app, 0) + 1
        if len(chosen) >= args.n:
            break

    sel = {"seed": args.seed, "per_app_cap": args.per_app, "n_requested": args.n, "n_selected": len(chosen),
           "pool_size": len(pool), "excluded_task_ids_used_before": sorted(used),
           "excluded_scenarios": sorted(used_scenarios), "excluded_invalid_reference": sorted(invalid),
           "apps": per_app, "example_apps_in_prompt": sorted(EXAMPLE_APPS),
           "tasks": [{"task_id": r["task_id"], "split": r.get("split"), "app": primary_app(r),
                      "expected_changed_models": r.get("expected_changed_models"),
                      "instruction": r.get("instruction")} for r in chosen]}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(sel, indent=2))
    print(json.dumps({k: sel[k] for k in ("seed", "n_selected", "pool_size", "apps")}, indent=1))
    for t in sel["tasks"]:
        print(f"  {t['task_id']:11s} {t['split']:5s} {t['app']:12s} {t['instruction'][:80]}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
