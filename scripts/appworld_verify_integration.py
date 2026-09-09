#!/usr/bin/env python3
"""Phase B step 1-2: verify the AppWorld integration against the installation.

MASTER_PROMPT: "Verify current AppWorld installation/API before coding assumptions."
This makes that verification a reproducible artifact rather than a console session,
so it can be re-run after an AppWorld upgrade and cited in the paper.

Checks, in order:

    1. installed version, splits, and the public method signatures we depend on
    2. task loading and the compiler-facing state view
    3. tool/API access through the agent interface (`world.execute`)
    4. save_state -> mutate -> load_state restores the world **exactly**
    5. final-state inspection: the record-level delta a contract is evaluated on
    6. the official evaluator, in-process and out-of-process, cross-checked

    python scripts/appworld_verify_integration.py --task-id b7a9ee9_1
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

from effectgate.adapters.appworld_adapter import (  # noqa: E402
    AppWorldAdapter, AppWorldError, side_effecting_tasks,
)

OUT = REPO / "results" / "appworld" / "api_verification.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", default="b7a9ee9_1",
                    help="a side-effecting task with a valid reference solution")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    adapter = AppWorldAdapter()
    if not adapter.available():
        print("[fatal] AppWorld is not installed. Run scripts/install_appworld.sh")
        return 2

    report: dict = {"verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "task_id": args.task_id, "checks": {}}

    def record(name: str, fn):
        t0 = time.perf_counter()
        try:
            value = fn()
            entry = {"ok": True, **value}
        except Exception as exc:  # noqa: BLE001
            entry = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:1200]}
        entry["wall_s"] = round(time.perf_counter() - t0, 1)
        report["checks"][name] = entry
        flag = "ok  " if entry["ok"] else "FAIL"
        print(f"[{flag}] {name} ({entry['wall_s']}s)", flush=True)
        if not entry["ok"]:
            print("       " + str(entry["error"])[:400], flush=True)
        return entry

    # 1 -------------------------------------------------------------- API surface
    api = record("1_api_surface", lambda: {k: v for k, v in adapter.verify_api().items()
                                           if not k.startswith("_")})

    # 2 --------------------------------------------------- task + state loading
    def check_state():
        st = adapter.get_state(args.task_id)
        counts = st["state"]["counts"]
        n_tables = sum(len(v) for v in counts.values())
        n_rows = sum(sum(t.values()) for t in counts.values())
        return {"instruction": st["instruction"][:200],
                "n_apps": len(counts), "n_tables": n_tables, "n_rows_in_scope": n_rows,
                "detailed_apps": st["prompt_state"]["detailed_apps"],
                "prompt_view_bytes": len(json.dumps(st["prompt_state"])),
                "eval_view_bytes": len(json.dumps(st["state"])),
                "state_fetch_s": round(st["_wall_time_s"], 1)}

    record("2_task_and_state_loading", check_state)

    # 3,4,5,6 ------------------------------------- replay: tools, restore, delta
    def check_replay():
        # Two runs of the same reference solution: the first from the fresh world, the
        # second from the *restored* snapshot. If restore is exact and execution is
        # deterministic, the two deltas must be identical.
        r = adapter.replay(args.task_id, [{"name": "reference_1", "code": "reference"},
                                          {"name": "reference_2", "code": "reference"}])
        runs = r["candidates"]
        first, second = runs[0], runs[1]
        return {
            "n_candidates": len(runs),
            "snapshot_id": r["snapshot_id"],
            "tool_access_ok": first["execution_error"] is None,
            "execution_output_tail": (first["outputs"][-1] if first["outputs"] else "")[-160:],
            "delta_run1": {k: {kk: vv for kk, vv in v.items() if kk.startswith("n_")}
                           for k, v in first["delta"].items()},
            "delta_run2": {k: {kk: vv for kk, vv in v.items() if kk.startswith("n_")}
                           for k, v in second["delta"].items()},
            "restore_exact": second["restore_exact"],
            "residual_delta_after_restore": second["residual_delta_after_restore"],
            "replay_reproduces_delta": first["delta"] == second["delta"],
            "official_success_run1": first["official_evaluation"].get("success"),
            "official_success_run2": second["official_evaluation"].get("success"),
            "replay_reproduces_official_verdict":
                first["official_evaluation"].get("success")
                == second["official_evaluation"].get("success"),
            # Not a warning to be silenced: AppWorld's clock freezer is left unbalanced
            # by load_state, so teardown raises. Recorded because a harness that did not
            # guard it would lose a completed result to a teardown exception.
            "close_error": r.get("close_error"),
        }

    replay = record("3-5_tools_restore_and_final_state", check_replay)

    # 6 -------------------------------------------- official evaluator, both paths
    def check_evaluator():
        ref = adapter.reference_run(args.task_id)
        out_of_process = adapter.evaluate_task(args.task_id, experiment="effectgate_reference")
        off = out_of_process["official_evaluation"]
        in_proc = ref.official_evaluation
        return {
            "in_process_success": in_proc.get("success"),
            "out_of_process_success": off.get("success"),
            "verdicts_match": in_proc.get("success") == off.get("success"),
            "num_tests_match": in_proc.get("num_tests") == off.get("num_tests"),
            "requirements_match":
                sorted(p["requirement"] for p in (in_proc.get("passes") or []))
                == sorted(p["requirement"] for p in (off.get("passes") or [])),
            "requirements": [r["requirement"] for r in ref.official_requirements()],
            "n_state_requirements": sum(
                1 for r in ref.official_requirements()
                if "assert answers match" not in r["requirement"].lower()),
        }

    record("6_official_evaluator_cross_check", check_evaluator)

    report["all_ok"] = all(c["ok"] for c in report["checks"].values())
    report["summary"] = {
        "appworld_version": api.get("appworld_version"),
        "native_snapshot_restore": bool(api.get("has_save_state") and api.get("has_load_state")),
        "restore_is_exact": replay.get("restore_exact"),
        "replay_is_deterministic": replay.get("replay_reproduces_delta"),
        "official_evaluator_cross_checks":
            report["checks"]["6_official_evaluator_cross_check"].get("verdicts_match"),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("\n=== summary ===")
    print(json.dumps(report["summary"], indent=2, default=str))
    print(f"\nwrote {args.out}")
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
