#!/usr/bin/env python3
"""Do AppWorld's own ground-truth solutions pass AppWorld's own evaluator?

Phase B rests on this. The "known-good" final state that self-validation is allowed to
see, and that every contract is pre-flighted against, is produced by executing the
task's shipped `compiled_solution.py`. If that execution does not satisfy the official
evaluator, then for that task we do not have a known-good state at all, and any
contract judged against it is being judged against an unverified reference.

Observed on `3ab5b8b_1`: the shipped solution ran without error, added 16
`spotify.UserDownloadedSong` records, and still failed
`assert added downloaded song_ids match private_data.to_download_song_ids`.

No LLM involved; ~20 s per task. Tasks that fail here are excluded from contract
scoring rather than silently counted as EffectGate disagreements.

    python scripts/appworld_reference_validity.py --tasks 20
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from effectgate.adapters.appworld_adapter import (  # noqa: E402
    AppWorldAdapter, AppWorldError, side_effecting_tasks, simplest_tasks,
)
from effectgate.experiments.recorder import JsonlRecorder  # noqa: E402

OUT = REPO / "results" / "appworld"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=20)
    ap.add_argument("--all", action="store_true", help="every side-effecting task")
    ap.add_argument("--out", default=str(OUT / "reference_validity.jsonl"))
    args = ap.parse_args()

    adapter = AppWorldAdapter()
    if not adapter.available():
        print("[fatal] AppWorld is not installed.")
        return 2

    chosen = side_effecting_tasks() if args.all else simplest_tasks(args.tasks)
    rec = JsonlRecorder(Path(args.out), key="task_id")

    for i, t in enumerate(chosen, 1):
        tid = t["task_id"]
        if rec.done(tid):
            continue
        row = {"task_id": tid, "split": t.get("split"),
               "instruction": t.get("instruction", "")[:200],
               "expected_changed_models": t.get("expected_changed_models")}
        try:
            run = adapter.reference_run(tid)
            failed = [r["requirement"] for r in run.official_requirements() if not r["passed"]]
            row.update({
                "official_success": run.official_success,
                "changed_tables": run.changed_tables,
                "delta_counts": {k: {kk: vv for kk, vv in v.items() if kk.startswith("n_")}
                                 for k, v in run.delta.items()},
                "execution_error": run.execution_error,
                "n_failed_requirements": len(failed),
                "failed_requirements": failed[:6],
                "wall_s": run.wall_time_s,
            })
        except AppWorldError as exc:
            row["adapter_error"] = str(exc)[:800]
        rec.append(row)
        mark = "OK  " if row.get("official_success") else "FAIL"
        print(f"[{i}/{len(chosen)}] {mark} {tid:14s} changed={row.get('changed_tables')}",
              flush=True)
        for f in (row.get("failed_requirements") or [])[:2]:
            print(f"           - {f[:100]}", flush=True)

    rows = [json.loads(x) for x in Path(args.out).read_text().splitlines() if x.strip()]
    ok = [r for r in rows if r.get("official_success") is True]
    bad = [r for r in rows if r.get("official_success") is False]
    err = [r for r in rows if r.get("adapter_error")]

    print(f"\n=== reference validity over {len(rows)} tasks ===")
    print(f"  ground truth passes its own evaluator : {len(ok)} ({len(ok)/max(len(rows),1):.0%})")
    print(f"  ground truth FAILS its own evaluator  : {len(bad)}")
    print(f"  harness/adapter errors                : {len(err)}")
    if bad:
        print("\n  tasks with no trustworthy known-good state (excluded from scoring):")
        for r in bad:
            print(f"    {r['task_id']:14s} {r.get('n_failed_requirements')} failed req(s)")
        why = Counter(f.split("(")[0].strip()[:70]
                      for r in bad for f in (r.get("failed_requirements") or []))
        print("\n  most common failing requirements:")
        for w, n in why.most_common(8):
            print(f"    {n:3d}  {w}")

    summary = {
        "n_tasks": len(rows), "n_reference_valid": len(ok),
        "n_reference_invalid": len(bad), "n_errors": len(err),
        "reference_valid_rate": len(ok) / len(rows) if rows else None,
        "invalid_task_ids": [r["task_id"] for r in bad],
    }
    (OUT / "reference_validity_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}/reference_validity_summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
