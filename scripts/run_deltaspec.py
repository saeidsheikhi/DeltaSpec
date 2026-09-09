#!/usr/bin/env python3
"""DeltaSpec end to end on AppWorld tasks, with externally labelled variants and baselines.

Per task:
  1. variants   reference + skip_k / dup / no-op / extra_read / foreign executions from one
                restored world, each with the OFFICIAL evaluator's verdict (state requirements)
  2. mine       candidate facts from the reference transition (deterministic)
  3. label      one short LLM call: required / side_effect / incidental / unsure per fact
  4. assemble   DSL v0.5 contract; abstain if no effect fact is required
  5. evaluate   the contract on the reference (must accept), on every variant (scored
                against the official verdict), and on synthetic state mutants (kill rate)
  6. baselines  on the same variants: state-hash equality (tier 0), and -- if a frozen
                one-shot contract exists for the task -- that contract

Metrics per task: usable, auto_accept, agree(reference), abstain, incorrect_block,
unsafe_promotion (accepts a variant the official evaluator fails), false_block_variant
(rejects a variant the official evaluator passes), kill rate on official-failed variants
and on synthetic mutants, alternative tolerance (extra_read), runtime.

    python scripts/run_deltaspec.py --task-ids a,b,c --model qwen3.5:latest --label deltaspec_dev --out results/deltaspec_dev
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

from deltaspec.assemble import assemble  # noqa: E402
from deltaspec.label import PROMPT, label_facts  # noqa: E402
from deltaspec.mine import mine_facts  # noqa: E402
from deltaspec.mutants import build_mutants  # noqa: E402
from effectgate.adapters.appworld_adapter import AppWorldAdapter, AppWorldError, side_effecting_tasks  # noqa: E402
from effectgate.contracts.evaluator import evaluate_contract  # noqa: E402
from effectgate.contracts.io import contract_from_dict, contract_hash  # noqa: E402
from effectgate.contracts.schema import lint_contract  # noqa: E402
from effectgate.experiments.recorder import JsonlRecorder, run_manifest  # noqa: E402
from effectgate.providers.ollama import OllamaProvider  # noqa: E402


def official_state_pass(ev: dict) -> bool | None:
    reqs = [(p.get("requirement", ""), True) for p in (ev.get("passes") or [])] + \
           [(p.get("requirement", ""), False) for p in (ev.get("failures") or [])]
    state = [ok for req, ok in reqs if "assert answers match" not in req.lower()]
    return all(state) if state else None


def hash_equal(a: dict, b: dict) -> bool:
    return json.dumps(a.get("records"), sort_keys=True) == json.dumps(b.get("records"), sort_keys=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-ids", default="")
    ap.add_argument("--tasks", type=int, default=0)
    ap.add_argument("--model", default="qwen3.5:latest")
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--prompt", default=str(PROMPT))
    ap.add_argument("--n-skip", type=int, default=2)
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--no-foreign", action="store_true")
    ap.add_argument("--baseline-contracts", default=None,
                    help="JSONL of a frozen one-shot condition (rows with contract_dict) to evaluate on the same variants")
    ap.add_argument("--baseline-condition", default=None)
    args = ap.parse_args()
    baseline: dict[str, dict] = {}
    if args.baseline_contracts:
        for line in Path(args.baseline_contracts).read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if (not args.baseline_condition or r.get("condition") == args.baseline_condition) and r.get("status") == "ok" and r.get("contract_dict"):
                    baseline[r["task_id"]] = r["contract_dict"]

    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    rec = JsonlRecorder(out / "runs.jsonl", key="key")
    calls = JsonlRecorder(out / "provider_calls.jsonl")
    adapter = AppWorldAdapter(); provider = OllamaProvider()
    prompt_text = Path(args.prompt).read_text(encoding="utf-8")
    manifest = run_manifest({"phase": "deltaspec", "condition": args.label, "model": args.model,
                             "prompt": str(Path(args.prompt).resolve().relative_to(REPO)),
                             "prompt_sha256": hashlib.sha256(prompt_text.encode()).hexdigest(),
                             "n_skip": args.n_skip, "num_ctx": args.num_ctx})
    (out / f"manifest.{args.label}.json").write_text(json.dumps(manifest, indent=2))
    (out / "prompts").mkdir(exist_ok=True); (out / "prompts" / f"{args.label}.labeler.md").write_text(prompt_text)

    survey = {r["task_id"]: r for r in side_effecting_tasks()}
    if args.task_ids:
        ids = [t.strip() for t in args.task_ids.split(",") if t.strip()]
    else:
        ids = [r["task_id"] for r in side_effecting_tasks()][: args.tasks]
    # a foreign task for the wrong-effect variant: the next side-effecting task in a different scenario
    all_ids = [r["task_id"] for r in side_effecting_tasks()]

    for i, tid in enumerate(ids, 1):
        key = f"{args.label}|{args.model}|{tid}"
        if rec.done(key):
            print(f"[{i}/{len(ids)}] {tid}: done, skipping", flush=True); continue
        row = {"key": key, "task_id": tid, "condition": args.label, "model": args.model,
               "instruction": survey.get(tid, {}).get("instruction", "")}
        t0 = time.perf_counter()
        try:
            st = adapter.get_state(tid, field_aware=True)
            apps = [a for a in st["allowed_apps"] if a not in ("api_docs", "supervisor")]
            row["instruction"] = st["instruction"]
            foreign = None
            if not args.no_foreign:
                scen = tid.rsplit("_", 1)[0]
                foreign = next((x for x in all_ids if x.rsplit("_", 1)[0] != scen), None)
            t1 = time.perf_counter()
            V = adapter.variants(tid, apps, n_skip=args.n_skip, foreign_task_id=foreign, field_aware=True)
            row["variants_s"] = time.perf_counter() - t1
            pre = V["pre_state"]
            cands = {c["candidate"]: c for c in V["candidates"]}
            ref = cands["reference"]
            post, delta = ref["post_state"], ref["delta"]
            row["official_reference"] = official_state_pass(ref["official_evaluation"])
            row["delta"] = {k: {kk: vv for kk, vv in v.items() if kk.startswith("n_")} for k, v in delta.items()}
            row["variant_official"] = {n: official_state_pass(c["official_evaluation"]) for n, c in cands.items()}
            row["variant_restore_exact"] = {n: c.get("restore_exact") for n, c in cands.items()}
            row["variant_exec_error"] = {n: c.get("execution_error") for n, c in cands.items()}
            if not delta:
                row["status"] = "abstain"; row["reason"] = "reference changed nothing"
                raise StopIteration
            if row["official_reference"] is not True:
                row["status"] = "invalid_reference"; row["reason"] = "reference fails the official evaluator"
                raise StopIteration

            # 2. mine -- and re-check every fact on the reference transition. A fact is
            #    true of the reference by construction; if the evaluator disagrees, the
            #    miner and evaluator differ on some real-data corner and the fact must not
            #    reach a contract. Logged, never silently kept.
            facts_all = mine_facts(pre, post, delta)
            facts, dropped = [], []
            for f in facts_all:
                probe = {"contract_version": "0.5", "task_id": tid,
                         "required": f.predicates if f.clause == "required" else
                                     [{"kind": "delta", "path": "/records", "op": "changed", "value": None,
                                       "description": "", "critical": True}],
                         "forbidden": f.predicates if f.clause == "forbidden" else [],
                         "invariants": [], "alternatives": [], "assumptions": []}
                try:
                    ok = evaluate_contract(contract_from_dict(probe), pre, post).passed
                except Exception as exc:  # noqa: BLE001
                    ok = False; f.detail["probe_error"] = str(exc)[:120]
                (facts if ok else dropped).append(f)
            row["n_facts"] = len(facts); row["n_facts_dropped_by_probe"] = len(dropped)
            row["facts"] = [f.to_dict() for f in facts]
            row["facts_dropped"] = [f.to_dict() for f in dropped]
            if not facts:
                row["status"] = "abstain"; row["reason"] = "no fact survived the reference probe"
                raise StopIteration
            # 3. label
            t2 = time.perf_counter()
            L = label_facts(provider, args.model, tid, st["instruction"], facts, delta,
                            prompt_path=args.prompt, num_ctx=args.num_ctx)
            row["label_s"] = time.perf_counter() - t2
            if L.llm is not None:
                calls.append(L.llm.call_record(stage="deltaspec_label", task_id=tid, parse_status="ok" if L.ok else "error"))
            row["labels"] = L.labels; row["label_missing"] = L.n_missing
            if not L.ok:
                row["status"] = "abstain"; row["reason"] = f"labeller: {L.error}"
                raise StopIteration
            # 4. assemble
            A = assemble(tid, facts, L.labels, delta)
            row["assembled"] = A.to_dict()
            if A.status != "ok":
                row["status"] = "abstain"; row["reason"] = A.reason
                raise StopIteration
            contract = contract_from_dict(A.contract)
            lint = lint_contract(contract, pre)
            row["contract_dict"] = A.contract; row["contract_hash"] = contract_hash(contract)
            row["lint_ok"] = lint.ok; row["lint_errors"] = lint.errors[:5]
            if not lint.ok:
                row["status"] = "abstain"; row["reason"] = "assembled contract failed lint: " + "; ".join(lint.errors[:2])
                raise StopIteration
            # 5. evaluate
            r_ref = evaluate_contract(contract, pre, post)
            row["accepts_reference"] = r_ref.passed
            row["status"] = "ok"
            verdicts, hash_verdicts = {}, {}
            for name, c in cands.items():
                res = evaluate_contract(contract, pre, c["post_state"])
                verdicts[name] = {"pass": res.passed, "status": res.status,
                                  "violations": [(o.group, o.predicate.op, o.predicate.table or o.predicate.path) for o in res.violations()][:4]}
                hash_verdicts[name] = hash_equal(c["post_state"], post)
            row["verdicts"] = verdicts; row["baseline_hash_equal"] = hash_verdicts
            # output-text baseline: the candidate printed exactly what the reference printed
            ref_out = "\n".join(ref.get("outputs") or [])
            row["baseline_output_equal"] = {n: ("\n".join(c.get("outputs") or []) == ref_out) for n, c in cands.items()}
            # one-shot frozen contract baseline on the same variants, if available for the task
            if tid in baseline:
                try:
                    bc = contract_from_dict(baseline[tid])
                    row["baseline_oneshot"] = {n: evaluate_contract(bc, pre, c["post_state"]).passed for n, c in cands.items()}
                except Exception as exc:  # noqa: BLE001
                    row["baseline_oneshot"] = {"error": str(exc)[:200]}
            # scoring vs official
            unsafe = [n for n, c in cands.items() if row["variant_official"][n] is False and verdicts[n]["pass"]]
            false_block = [n for n, c in cands.items() if row["variant_official"][n] is True and not verdicts[n]["pass"]]
            killed = [n for n, c in cands.items() if row["variant_official"][n] is False and not verdicts[n]["pass"]]
            row["unsafe_promotions"] = unsafe; row["false_block_variants"] = false_block; row["killed_variants"] = killed
            n_neg = sum(1 for n in cands if row["variant_official"][n] is False)
            row["variant_kill_rate"] = (len(killed) / n_neg) if n_neg else None
            row["alt_tolerated"] = verdicts.get("extra_read", {}).get("pass")
            row["hash_baseline"] = {"unsafe": [n for n in cands if row["variant_official"][n] is False and hash_verdicts[n]],
                                    "false_block": [n for n in cands if row["variant_official"][n] is True and not hash_verdicts[n]]}
            oe = row["baseline_output_equal"]
            row["output_baseline"] = {"unsafe": [n for n in cands if row["variant_official"][n] is False and oe[n]],
                                      "false_block": [n for n in cands if row["variant_official"][n] is True and not oe[n]]}
            bo = row.get("baseline_oneshot")
            if isinstance(bo, dict) and "error" not in bo:
                row["oneshot_baseline"] = {"unsafe": [n for n in cands if row["variant_official"][n] is False and bo.get(n)],
                                           "false_block": [n for n in cands if row["variant_official"][n] is True and not bo.get(n)],
                                           "accepts_reference": bo.get("reference")}
            muts = build_mutants(pre, post, delta)
            mres = {m["name"]: not evaluate_contract(contract, pre, m["state"]).passed for m in muts}
            row["synthetic_mutants"] = mres
            row["synthetic_kill_rate"] = (sum(mres.values()) / len(mres)) if mres else None
        except StopIteration:
            pass
        except AppWorldError as exc:
            row["status"] = "adapter_error"; row["reason"] = str(exc)[:600]
        except Exception as exc:  # noqa: BLE001
            row["status"] = "harness_error"; row["reason"] = f"{type(exc).__name__}: {exc}"[:600]
        row["total_s"] = time.perf_counter() - t0
        rec.append(row)
        print(f"[{i}/{len(ids)}] {tid}: {row.get('status')} {row.get('reason') or ''}", flush=True)
        print(f"    facts={row.get('n_facts')} required={ (row.get('assembled') or {}).get('n_required')} "
              f"accepts_ref={row.get('accepts_reference')} kill(variants)={row.get('variant_kill_rate')} "
              f"kill(synthetic)={row.get('synthetic_kill_rate')} unsafe={row.get('unsafe_promotions')} "
              f"false_block={row.get('false_block_variants')} alt={row.get('alt_tolerated')} "
              f"({row.get('total_s', 0):.0f}s: variants {row.get('variants_s', 0):.0f}s, label {row.get('label_s', 0):.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
