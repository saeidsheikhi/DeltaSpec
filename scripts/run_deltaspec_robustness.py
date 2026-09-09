#!/usr/bin/env python3
"""SUPPLEMENTARY ANALYSIS on the frozen DeltaSpec v1 system: labeller-model robustness and
ablations, evaluated on the same generic execution variants (official labels) and synthetic
mutants as the primary run. The frozen modules (`deltaspec.mine/label/assemble/mutants`,
prompt) are imported unchanged; this script only *combines* them differently.

Per task, variants are executed once; every condition is scored on the same candidate states:

  v1_recorded   the contract the frozen primary/retry/rof run recorded (like-for-like row)
  relabel:<m>   frozen miner + frozen prompt/grammar, labeller model m (robustness axis;
                includes the frozen model itself as a determinism check)
  abl_no_exact  v1 labels, exact-count predicates removed        (ablation: exactness)
  abl_no_scope  v1 labels, scope invariant removed               (ablation: scope)
  abl_all_req   every mined fact required, no LLM                (ablation: labelling)
  abl_tables    only table-level effects required, no LLM        (ablation: labelling)
  abl_no_prohib v1 labels, forbidden clause removed              (ablation: prohibitions)

Harness note: runs under the worker as frozen in `deltaspec_v1_rof` (raise_on_failure=False),
so official variant labels can differ from the primary run for a task whose reference probes
error responses; the `v1_recorded` row is the in-harness reference point for every comparison.

    OLLAMA_THINK=false python scripts/run_deltaspec_robustness.py \
        --task-ids-from results/deltaspec_fresh/VALIDATION_SET_FROZEN.json \
        --labellers qwen3.5:latest,gemma3:27b,phi4:latest --label supp_robustness \
        --out results/deltaspec_supp
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src")); sys.path.insert(0, str(REPO / "scripts"))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO / ".env")
from deltaspec.assemble import assemble  # noqa: E402
from deltaspec.label import PROMPT, label_facts  # noqa: E402
from deltaspec.mine import mine_facts  # noqa: E402
from deltaspec.mutants import build_mutants  # noqa: E402
from effectgate.adapters.appworld_adapter import AppWorldAdapter, side_effecting_tasks  # noqa: E402
from effectgate.contracts.evaluator import evaluate_contract  # noqa: E402
from effectgate.contracts.io import contract_from_dict, contract_hash  # noqa: E402
from effectgate.contracts.schema import lint_contract  # noqa: E402
from effectgate.experiments.recorder import JsonlRecorder, run_manifest  # noqa: E402
from effectgate.providers.ollama import OllamaProvider  # noqa: E402
from run_deltaspec import hash_equal, official_state_pass  # noqa: E402

EXACT_OPS = {"added_count_eq", "removed_count_eq", "updated_count_eq"}
TABLE_KINDS = {"table_added", "table_removed", "table_updated"}


def recorded_v1_contracts(path: Path, conds: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("condition") in conds and r.get("status") == "ok" and r.get("contract_dict"):
            out[r["task_id"]] = {"contract": r["contract_dict"], "labels": (r.get("assembled") or {}).get("labels") or {},
                                 "condition": r["condition"]}
    return out


def probe_facts(tid, pre, post, delta):
    facts, dropped = [], []
    for f in mine_facts(pre, post, delta):
        probe = {"contract_version": "0.5", "task_id": tid,
                 "required": f.predicates if f.clause == "required" else
                             [{"kind": "delta", "path": "/records", "op": "changed", "value": None, "description": "", "critical": True}],
                 "forbidden": f.predicates if f.clause == "forbidden" else [], "invariants": [], "alternatives": [], "assumptions": []}
        try:
            ok = evaluate_contract(contract_from_dict(probe), pre, post).passed
        except Exception:  # noqa: BLE001
            ok = False
        (facts if ok else dropped).append(f)
    return facts, dropped


def score(contract_dict, tid, pre, post, cands, official, muts):
    """Evaluate one contract on reference, variants and synthetic mutants."""
    try:
        c = contract_from_dict(contract_dict)
    except Exception as exc:  # noqa: BLE001
        return {"status": "invalid", "error": str(exc)[:200]}
    lint = lint_contract(c, pre)
    if not lint.ok:
        return {"status": "lint_fail", "error": "; ".join(lint.errors[:2])}
    v = {n: evaluate_contract(c, pre, x["post_state"]).passed for n, x in cands.items()}
    m = {x["name"]: not evaluate_contract(c, pre, x["state"]).passed for x in muts}
    return {"status": "ok", "contract_hash": contract_hash(c), "accepts_reference": v.get("reference"),
            "verdicts": v,
            "unsafe": [n for n in cands if official[n] is False and v[n]],
            "killed": [n for n in cands if official[n] is False and not v[n]],
            "false_block": [n for n in cands if official[n] is True and n != "reference" and not v[n]],
            "synthetic": m, "n_required": len(contract_dict.get("required") or []),
            "n_forbidden": len(contract_dict.get("forbidden") or [])}


def strip(contract: dict, *, no_exact=False, no_scope=False, no_prohib=False) -> dict:
    c = json.loads(json.dumps(contract))
    if no_exact:
        c["required"] = [p for p in c["required"] if p.get("op") not in EXACT_OPS]
    if no_scope:
        c["invariants"] = []
    if no_prohib:
        c["forbidden"] = []
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-ids", default=None); ap.add_argument("--task-ids-from", default=None)
    ap.add_argument("--labellers", default="qwen3.5:latest,gemma3:27b,phi4:latest")
    ap.add_argument("--label", default="supp_robustness"); ap.add_argument("--out", default="results/deltaspec_supp")
    ap.add_argument("--recorded", default="results/deltaspec_fresh/runs.jsonl")
    ap.add_argument("--recorded-conditions", default="deltaspec_fresh,deltaspec_fresh_retry_transport,deltaspec_fresh_rof")
    ap.add_argument("--n-skip", type=int, default=2); ap.add_argument("--num-ctx", type=int, default=8192)
    args = ap.parse_args()
    ids = args.task_ids.split(",") if args.task_ids else json.load(open(args.task_ids_from))["task_ids"]
    labellers = [m for m in args.labellers.split(",") if m]
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    rec = JsonlRecorder(out / "robustness_runs.jsonl", key="key"); calls = JsonlRecorder(out / "provider_calls.jsonl")
    v1 = recorded_v1_contracts(Path(args.recorded), args.recorded_conditions.split(","))
    manifest = run_manifest({"phase": "deltaspec_supplementary", "condition": args.label, "labellers": labellers,
                             "prompt": str(PROMPT.relative_to(REPO)) if hasattr(PROMPT, "relative_to") else str(PROMPT),
                             "frozen_system": "results/frozen_systems/deltaspec_v1_rof.json",
                             "recorded_from": args.recorded, "recorded_conditions": args.recorded_conditions,
                             "n_tasks": len(ids), "n_skip": args.n_skip, "num_ctx": args.num_ctx, "seed": 42})
    (out / f"manifest.{args.label}.json").write_text(json.dumps(manifest, indent=2, default=str))
    adapter = AppWorldAdapter(); provider = OllamaProvider()
    all_ids = [r["task_id"] for r in side_effecting_tasks()]
    for i, tid in enumerate(ids, 1):
        key = f"{args.label}|{tid}"
        if rec.done(key):
            print(f"[{i}/{len(ids)}] {tid}: done, skipping", flush=True); continue
        row = {"key": key, "task_id": tid, "condition": args.label, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "conditions": {}}
        t0 = time.perf_counter()
        try:
            st = adapter.get_state(tid, field_aware=True)
            apps = [a for a in st["allowed_apps"] if a not in ("api_docs", "supervisor")]
            scen = tid.rsplit("_", 1)[0]
            foreign = next((x for x in all_ids if x.rsplit("_", 1)[0] != scen), None)
            V = adapter.variants(tid, apps, n_skip=args.n_skip, foreign_task_id=foreign, field_aware=True)
            row["variants_s"] = time.perf_counter() - t0
            pre = V["pre_state"]; cands = {c["candidate"]: c for c in V["candidates"]}
            ref = cands["reference"]; post, delta = ref["post_state"], ref["delta"]
            official = {n: official_state_pass(c["official_evaluation"]) for n, c in cands.items()}
            row["variant_official"] = official; row["official_reference"] = official["reference"]
            row["instruction"] = st["instruction"]
            if not delta or official["reference"] is not True:
                row["status"] = "invalid_reference"; rec.append(row)
                print(f"[{i}/{len(ids)}] {tid}: invalid reference in this harness", flush=True); continue
            muts = build_mutants(pre, post, delta)
            row["baseline_hash_equal"] = {n: hash_equal(c["post_state"], post) for n, c in cands.items()}
            ref_out = "\n".join(ref.get("outputs") or [])
            row["baseline_output_equal"] = {n: ("\n".join(c.get("outputs") or []) == ref_out) for n, c in cands.items()}
            facts, dropped = probe_facts(tid, pre, post, delta)
            row["n_facts"] = len(facts); row["facts"] = [f.to_dict() for f in facts]
            conds = row["conditions"]
            # --- like-for-like: the recorded frozen contract ---
            if tid in v1:
                conds["v1_recorded"] = score(v1[tid]["contract"], tid, pre, post, cands, official, muts)
                conds["v1_recorded"]["source_condition"] = v1[tid]["condition"]
                # --- ablations on the recorded contract ---
                for name, kw in (("abl_no_exact", {"no_exact": True}), ("abl_no_scope", {"no_scope": True}),
                                 ("abl_no_prohib", {"no_prohib": True})):
                    c = strip(v1[tid]["contract"], **kw)
                    conds[name] = score(c, tid, pre, post, cands, official, muts) if c["required"] else {"status": "abstain", "error": "no required predicate left"}
            else:
                conds["v1_recorded"] = {"status": "no_recorded_contract"}
            # --- LLM-free ablations ---
            A = assemble(tid, facts, {f.fid: "required" for f in facts}, delta)
            conds["abl_all_req"] = score(A.contract, tid, pre, post, cands, official, muts) if A.status == "ok" else {"status": "abstain"}
            A = assemble(tid, facts, {f.fid: ("required" if f.kind in TABLE_KINDS else "incidental") for f in facts}, delta)
            conds["abl_tables"] = score(A.contract, tid, pre, post, cands, official, muts) if A.status == "ok" else {"status": "abstain"}
            # --- labeller robustness ---
            for m in labellers:
                t1 = time.perf_counter()
                L = label_facts(provider, m, tid, st["instruction"], facts, delta, prompt_path=PROMPT, num_ctx=args.num_ctx)
                if L.llm is not None:
                    calls.append(L.llm.call_record(stage="deltaspec_label_supp", task_id=tid, parse_status="ok" if L.ok else "error"))
                cname = f"relabel:{m}"
                if not L.ok:
                    conds[cname] = {"status": "abstain", "error": f"labeller: {L.error}"[:300], "label_s": time.perf_counter() - t1}; continue
                A = assemble(tid, facts, L.labels, delta)
                if A.status != "ok":
                    conds[cname] = {"status": "abstain", "error": A.reason, "labels": L.labels, "label_s": time.perf_counter() - t1}; continue
                conds[cname] = score(A.contract, tid, pre, post, cands, official, muts)
                conds[cname].update({"labels": L.labels, "notes": A.notes, "label_s": time.perf_counter() - t1,
                                     "same_labels_as_v1": (L.labels == v1[tid]["labels"]) if tid in v1 else None})
            row["status"] = "ok"
        except Exception as exc:  # noqa: BLE001
            row["status"] = "harness_error"; row["error"] = f"{type(exc).__name__}: {exc}"[:400]
        row["total_s"] = time.perf_counter() - t0
        rec.append(row)
        summ = {k: (f"unsafe={len(v.get('unsafe', []))} fb={len(v.get('false_block', []))} ref={v.get('accepts_reference')}" if v.get("status") == "ok" else v.get("status")) for k, v in row["conditions"].items()}
        print(f"[{i}/{len(ids)}] {tid}: {row['status']} {summ} ({row['total_s']:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
