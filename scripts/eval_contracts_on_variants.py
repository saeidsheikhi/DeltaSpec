#!/usr/bin/env python3
"""Post-hoc baseline: evaluate previously produced (frozen, one-shot) contracts on the same
generic execution variants that `run_deltaspec.py` uses, with the same official labels.

Used when the baseline contracts did not yet exist when the DeltaSpec run executed (the
frozen v0.6.1 compiler is run on the validation tasks *concurrently*). Variants are
deterministic functions of the task (reference code + wrapper prelude), so re-running them
here yields the same candidate states. LLM-free.

    python scripts/eval_contracts_on_variants.py --contracts results/appworld_linked_v061/sanity_runs.jsonl \
        --condition linked_v061_freshB --task-ids a,b,c --label oneshot_v061_on_variants \
        --out results/deltaspec_fresh/oneshot_on_variants.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src")); sys.path.insert(0, str(REPO / "scripts"))
from effectgate.adapters.appworld_adapter import AppWorldAdapter, side_effecting_tasks  # noqa: E402
from effectgate.contracts.evaluator import evaluate_contract  # noqa: E402
from effectgate.contracts.io import contract_from_dict, state_hash  # noqa: E402
from effectgate.experiments.recorder import JsonlRecorder  # noqa: E402
from run_deltaspec import official_state_pass  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contracts", required=True); ap.add_argument("--condition", required=True)
    ap.add_argument("--task-ids", required=True); ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--n-skip", type=int, default=2)
    args = ap.parse_args()
    rows = [json.loads(l) for l in Path(args.contracts).read_text().splitlines() if l.strip()]
    by_task = {r["task_id"]: r for r in rows if r.get("condition") == args.condition}
    ids = args.task_ids.split(",")
    rec = JsonlRecorder(Path(args.out), key="key")
    adapter = AppWorldAdapter()
    all_ids = [r["task_id"] for r in side_effecting_tasks()]
    for i, tid in enumerate(ids, 1):
        key = f"{args.label}|{tid}"
        if rec.done(key):
            print(f"[{i}/{len(ids)}] {tid}: done, skipping", flush=True); continue
        src = by_task.get(tid)
        row = {"key": key, "task_id": tid, "condition": args.label, "source_condition": args.condition,
               "source_status": (src or {}).get("status"), "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if not src or src.get("status") != "ok" or not src.get("contract_dict"):
            row["status"] = "no_contract"; rec.append(row)
            print(f"[{i}/{len(ids)}] {tid}: no usable one-shot contract ({row['source_status']})", flush=True); continue
        t0 = time.perf_counter()
        try:
            st = adapter.get_state(tid, field_aware=True)
            apps = [a for a in st["allowed_apps"] if a not in ("api_docs", "supervisor")]
            scen = tid.rsplit("_", 1)[0]
            foreign = next((x for x in all_ids if x.rsplit("_", 1)[0] != scen), None)
            V = adapter.variants(tid, apps, n_skip=args.n_skip, foreign_task_id=foreign, field_aware=True)
            pre = V["pre_state"]; cands = {c["candidate"]: c for c in V["candidates"]}
            official = {n: official_state_pass(c["official_evaluation"]) for n, c in cands.items()}
            contract = contract_from_dict(src["contract_dict"])
            verdicts = {n: evaluate_contract(contract, pre, c["post_state"]).passed for n, c in cands.items()}
            row.update({"status": "ok", "contract_hash": src.get("contract_hash"), "variant_official": official,
                        "verdicts": verdicts, "accepts_reference": verdicts.get("reference"),
                        "variant_post_hash": {n: state_hash(c["post_state"]["counts"]) for n, c in cands.items()},
                        "unsafe": [n for n in cands if official[n] is False and verdicts[n]],
                        "killed": [n for n in cands if official[n] is False and not verdicts[n]],
                        "false_block": [n for n in cands if official[n] is True and not verdicts[n]],
                        "total_s": time.perf_counter() - t0})
        except Exception as exc:  # noqa: BLE001
            row["status"] = "harness_error"; row["error"] = f"{type(exc).__name__}: {exc}"[:400]
        rec.append(row)
        print(f"[{i}/{len(ids)}] {tid}: {row['status']} unsafe={row.get('unsafe')} false_block={row.get('false_block')} killed={row.get('killed')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
