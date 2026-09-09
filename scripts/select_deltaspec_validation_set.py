#!/usr/bin/env python3
"""Select the DeltaSpec validation set from AppWorld train/dev, in two pre-registered tiers.

The side-effecting train/dev universe is 108 tasks in 36 scenarios. Every earlier EffectGate
pilot (v0.3 … v0.6.1, DeltaSpec development) touched 32 of the 36 scenarios, so a set that is
fresh at the *scenario* level can have at most the 4 untouched scenarios. Hence two tiers,
selected and recorded together BEFORE any execution and never changed afterwards:

* **Tier A — scenario-fresh.** Every valid task whose scenario (task-id prefix) appears in no
  prior result file and in no design set. No per-app cap (the pool is exhausted, not sampled).
  This is the clean generalisation estimate; its tasks are correlated within scenario, so
  per-scenario results are reported too.
* **Tier B — task-fresh, scenario-seen.** One never-executed task per already-seen scenario,
  seeded shuffle, capped at --n-b. The system has no task-specific content and the labeller
  prompt has no app examples, but the *author* saw failure modes of sibling tasks during
  development; Tier B is therefore the weaker estimate and is reported separately.

Both tiers exclude tasks with an invalid reference (ground truth fails AppWorld's own
evaluator, LLM-free check) and never touch test_normal / test_challenge.

    python scripts/select_deltaspec_validation_set.py --seed 42 --n-b 16 \
        --out results/deltaspec_fresh/VALIDATION_SET_FROZEN.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src")); sys.path.insert(0, str(REPO / "scripts"))
from effectgate.adapters.appworld_adapter import side_effecting_tasks  # noqa: E402
import select_fresh_validation_set as prior  # noqa: E402  (exclusion lists are shared)


def scen(tid: str) -> str:
    return tid.rsplit("_", 1)[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42); ap.add_argument("--n-b", type=int, default=16)
    ap.add_argument("--out", default=str(REPO / "results" / "deltaspec_fresh" / "VALIDATION_SET_FROZEN.json"))
    ap.add_argument("--frozen-system", default=None, help="frozen-system record the set is selected for (recorded)")
    args = ap.parse_args()
    out = Path(args.out)
    if out.exists():
        print(f"[fatal] {out} exists; a frozen validation set is never re-selected."); return 2

    used = prior.used_task_ids() | prior.DESIGN_TASK_IDS
    used_scen = {scen(t) for t in used}
    vpath = REPO / "results" / "appworld" / "reference_validity_summary.json"
    invalid = set(json.loads(vpath.read_text()).get("invalid_task_ids") or []) if vpath.exists() else set()
    td = [r for r in side_effecting_tasks() if r.get("split") in ("train", "dev")
          and r["task_id"] not in used and r["task_id"] not in invalid]
    tier_a = sorted([r for r in td if scen(r["task_id"]) not in used_scen], key=lambda r: r["task_id"])
    pool_b = [r for r in td if scen(r["task_id"]) in used_scen]
    rng = random.Random(args.seed); rng.shuffle(pool_b)
    tier_b, seen = [], set()
    for r in pool_b:
        if scen(r["task_id"]) in seen:
            continue
        tier_b.append(r); seen.add(scen(r["task_id"]))
        if len(tier_b) >= args.n_b:
            break

    def app(r):
        m = r.get("expected_changed_models") or []
        return m[0].split(".")[0] if m else "unknown"

    def rows(rs):
        return [{"task_id": r["task_id"], "scenario": scen(r["task_id"]), "split": r.get("split"), "app": app(r),
                 "expected_changed_models": r.get("expected_changed_models"), "instruction": r.get("instruction")} for r in rs]

    excl_files = {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest() for p in prior.PRIOR_RESULT_FILES if p.exists()}
    sel = {"selected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "seed": args.seed,
           "frozen_system": args.frozen_system, "procedure": __doc__,
           "universe": {"side_effecting_train_dev": len(side_effecting_tasks(split="train")) + len(side_effecting_tasks(split="dev")),
                        "scenarios": len({scen(r["task_id"]) for r in side_effecting_tasks() if r.get("split") in ("train", "dev")}),
                        "seen_scenarios": len(used_scen), "seen_task_ids": len(used), "invalid_reference": sorted(invalid)},
           "exclusion_files_sha256": excl_files, "excluded_task_ids": sorted(used),
           "tier_a": {"n": len(tier_a), "scenarios": sorted({scen(r["task_id"]) for r in tier_a}), "tasks": rows(tier_a)},
           "tier_b": {"n": len(tier_b), "n_requested": args.n_b, "pool": len(pool_b), "tasks": rows(tier_b)},
           "task_ids": [r["task_id"] for r in tier_a + tier_b]}
    out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(sel, indent=2))
    print(f"Tier A (scenario-fresh): {len(tier_a)} tasks in {len(sel['tier_a']['scenarios'])} scenarios")
    for t in sel["tier_a"]["tasks"]:
        print(f"  {t['task_id']:11s} {t['split']:5s} {t['app']:12s} {(t['instruction'] or '')[:80]}")
    print(f"Tier B (task-fresh, scenario-seen): {len(tier_b)} of pool {len(pool_b)}")
    for t in sel["tier_b"]["tasks"]:
        print(f"  {t['task_id']:11s} {t['split']:5s} {t['app']:12s} {(t['instruction'] or '')[:80]}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
