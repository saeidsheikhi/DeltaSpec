#!/usr/bin/env python3
"""Provenance check: the current AppWorld worker/adapter reproduce the state view that a
frozen system's rows recorded (pre-state hash over tier-0 counts, changed tables of the
reference delta). Used when the worker changed *after* a freeze (e.g. the additive
`variants` op added for DeltaSpec) to show the frozen systems' evidence is unaffected.
LLM-free.

    python scripts/worker_equivalence_check.py --rows results/appworld_linked_v061/sanity_runs.jsonl \
        --condition linked_v061_fresh --out results/deltaspec_dev/worker_equivalence_linked_v061_fresh.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from effectgate.adapters.appworld_adapter import AppWorldAdapter  # noqa: E402
from effectgate.contracts.io import state_hash  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True); ap.add_argument("--condition", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    rows = [json.loads(l) for l in Path(args.rows).read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r.get("condition") == args.condition and r.get("pre_state_hash")]
    if args.limit:
        rows = rows[: args.limit]
    ad = AppWorldAdapter(); out = []
    for r in rows:
        t0 = time.time()
        ref = ad.reference_run(r["task_id"])
        pre_h = state_hash(ref.pre_state["counts"]); post_h = state_hash(ref.post_state["counts"])
        changed = sorted(getattr(ref, "changed_tables", None) or (ref.delta.get("changed_tables") if isinstance(ref.delta, dict) else None) or [])
        rec_changed = sorted(r.get("changed_tables") or []) if r.get("changed_tables") else None
        rec = {"task_id": r["task_id"], "pre_hash_recorded": r["pre_state_hash"], "pre_hash_now": pre_h,
               "post_hash_recorded": r.get("post_state_hash"), "post_hash_now": post_h,
               "changed_tables_recorded": rec_changed, "changed_tables_now": changed,
               "pre_match": pre_h == r["pre_state_hash"], "post_match": post_h == r.get("post_state_hash"),
               "changed_match": (changed == rec_changed) if rec_changed is not None else None,
               "s": round(time.time() - t0, 1)}
        out.append(rec)
        print(f"{r['task_id']}: pre {rec['pre_match']} post {rec['post_match']} changed {rec['changed_match']} ({rec['s']} s)", flush=True)
    summary = {"condition": args.condition, "n": len(out),
               "pre_match": sum(x["pre_match"] for x in out), "post_match": sum(x["post_match"] for x in out),
               "changed_match": sum(bool(x["changed_match"]) for x in out if x["changed_match"] is not None),
               "rows": out}
    Path(args.out).write_text(json.dumps(summary, indent=2)); print(json.dumps({k: v for k, v in summary.items() if k != "rows"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
