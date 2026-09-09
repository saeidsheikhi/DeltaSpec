#!/usr/bin/env python3
"""Phase B step 1: a small end-to-end AppWorld integration check.

For each task:

    AppWorld task + initial state + table schemas
      -> EffectGate contract compilation (local Ollama)
      -> execute AppWorld's own ground-truth solution   (the known-good behaviour)
      -> EffectGate contract evaluation on (pre, post)
      -> AppWorld's official evaluator, unmodified      (independent gold)
      -> comparison, per requirement

The point is not to score EffectGate. It is to establish that the pipeline runs, and
to surface where an effect contract and AppWorld's official evaluator mean different
things. Disagreements are the output, not a failure.

    python scripts/run_appworld_sanity.py --tasks 3 --model phi4:latest
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

from effectgate.adapters.appworld_adapter import (  # noqa: E402
    AppWorldAdapter, AppWorldError, simplest_tasks,
)
from effectgate.contracts.compiler import REPAIR_PROMPT, compile_contract, schema_vocab  # noqa: E402
from effectgate.contracts.linker import link_contract  # noqa: E402
from effectgate.contracts.validator import (  # noqa: E402
    repair_contract, repair_syntax, score_contract,
)
from effectgate.contracts.evaluator import evaluate_contract  # noqa: E402
from effectgate.contracts.io import contract_hash, state_hash  # noqa: E402
from effectgate.experiments.recorder import JsonlRecorder, run_manifest  # noqa: E402
from effectgate.models import CONTRACT_VERSION  # noqa: E402
from effectgate.providers.ollama import OllamaProvider  # noqa: E402

PROMPT = REPO / "prompts" / "contract_compiler_appworld.md"
OUT = REPO / "results" / "appworld"

#: AppWorld requirement text that is about the *answer*, not about state. An effect
#: contract can say nothing about these, so they are excluded from agreement scoring
#: rather than counted as disagreements.
ANSWER_REQUIREMENTS = ("assert answers match",)


def archive_prompt(path: Path, out_dir: Path, label: str, role: str) -> dict:
    """Hash and copy a prompt file into the results directory.

    Prompts are mutable files referenced by *path* in the manifest, and this repository
    is not under version control (`git_commit: "not-a-git-repo"`). That combination
    already destroyed a headline result: `prompts/contract_compiler_appworld_v3.md` was
    edited on 2026-08-18 at 10:26, between the `..._refined` run (01:37) and the
    `..._refined_v2` run (10:56). The two were written up as the same prompt measured
    twice — the second labelled "this run measures variance" — but the recorded
    `prompt_hash` differs for every task in both runs, so they were different prompts,
    and the better one (14/15 agreement) no longer exists anywhere.

    A path is not provenance. The content is archived next to the results it produced.
    """
    text = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    archive = out_dir / "prompts" / f"{label}.{role}.md"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists() and archive.read_text(encoding="utf-8") != text:
        raise SystemExit(
            f"[fatal] condition {label!r} already has an archived {role} prompt with "
            f"different content ({archive}). A condition label identifies a system; "
            f"reusing it for a changed prompt is what made the v3 results "
            f"irreproducible. Use a new --label.")
    archive.write_text(text, encoding="utf-8")
    return {"path": _rel(path), "sha256": digest,
            "bytes": len(text.encode("utf-8")), "archived_to": _rel(archive)}


def _rel(path: Path) -> str:
    """Repo-relative where possible; absolute otherwise (a prompt may live outside)."""
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return str(path.resolve())


def classify_requirements(run) -> dict:
    """Split the official verdict into answer-level and state-level requirements."""
    answer, state = [], []
    for req in run.official_requirements():
        text = (req.get("requirement") or "").strip().lower()
        (answer if any(m in text for m in ANSWER_REQUIREMENTS) else state).append(req)
    return {
        "answer_requirements": answer,
        "state_requirements": state,
        "state_all_passed": all(r["passed"] for r in state) if state else None,
        "answer_all_passed": all(r["passed"] for r in answer) if answer else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=3)
    ap.add_argument("--task-ids", default="", help="explicit comma-separated task ids")
    ap.add_argument("--model", default="phi4:latest")
    ap.add_argument("--split", default=None)
    ap.add_argument("--repair-rounds", type=int, default=0,
                    help="known-good repair rounds when the contract rejects the "
                         "reference final state (0 disables, for a clean baseline)")
    ap.add_argument("--skip-invalid-reference", action="store_true", default=True,
                    help="skip tasks whose ground-truth solution fails AppWorld's own "
                         "evaluator: there is no known-good state to judge against")
    ap.add_argument("--prompt", default=str(PROMPT),
                    help="compiler system prompt. Changing it defines a new condition, "
                         "so pass --label and a separate --out as well")
    ap.add_argument("--repair-prompt", default=None,
                    help="repair system prompt (default: the DSL-v0.1 prompt built into "
                         "validator.py). A condition must pair a compiler prompt with a "
                         "repair prompt describing the same operator set")
    ap.add_argument("--label", default=None,
                    help="condition label recorded on every row (default: prompt stem). "
                         "Part of the resume key, so two conditions can share an --out "
                         "without silently deduplicating each other")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--num-ctx", type=int, default=8192,
                    help="Ollama context window. 8192 was the Phase B default; phi4 "
                         "supports 16384, and the longest observed call needed 8386 "
                         "prompt+completion tokens, so 8192 truncates it")
    ap.add_argument("--field-aware", action="store_true",
                    help="use the typed, owner-scoped field projection (DSL v0.4). A NEW "
                         "system: pair it with a v4 prompt and a fresh --label")
    ap.add_argument("--linker", action="store_true",
                    help="v0.6: run the deterministic contract linker after compilation "
                         "(bind, FK-link, simplify, reconcile). Replaces LLM repair; use "
                         "with --repair-rounds 0 and without --syntax-repair for the main condition")
    ap.add_argument("--vocab-grammar", action="store_true",
                    help="v0.6: enumerate the task's projected tables/fields into the decoding grammar")
    ap.add_argument("--syntax-repair", action="store_true",
                    help="give a contract that fails schema/static lint one mechanical "
                         "repair round. Off by default so the baseline is unchanged")
    args = ap.parse_args()

    prompt_path = Path(args.prompt)
    if not prompt_path.exists():
        print(f"[fatal] compiler prompt not found: {prompt_path}")
        return 2
    repair_prompt = Path(args.repair_prompt) if args.repair_prompt else REPAIR_PROMPT
    if not repair_prompt.exists():
        print(f"[fatal] repair prompt not found: {repair_prompt}")
        return 2
    label = args.label or prompt_path.stem

    adapter = AppWorldAdapter()
    if not adapter.available():
        print("[fatal] AppWorld is not installed. Run scripts/install_appworld.sh")
        return 2

    if args.task_ids:
        chosen = [{"task_id": t.strip(), "instruction": "", "n_expected_changed": None}
                  for t in args.task_ids.split(",") if t.strip()]
    else:
        chosen = simplest_tasks(args.tasks, split=args.split)
    if not chosen:
        print("[fatal] no side-effecting tasks found. Run scripts/appworld_survey.py first.")
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = JsonlRecorder(out_dir / "sanity_runs.jsonl", key="key")
    calls = JsonlRecorder(out_dir / "provider_calls.jsonl")
    provider = OllamaProvider()

    compiler_prompt_meta = archive_prompt(prompt_path, out_dir, label, "compiler")
    repair_prompt_meta = archive_prompt(repair_prompt, out_dir, label, "repair")
    manifest = run_manifest({
        "phase": "B_appworld_sanity", "condition": label,
        "compiler_model": args.model,
        "compiler_prompt": compiler_prompt_meta["path"],
        "repair_prompt": repair_prompt_meta["path"],
        "compiler_prompt_sha256": compiler_prompt_meta["sha256"],
        "repair_prompt_sha256": repair_prompt_meta["sha256"],
        "prompt_archive": [compiler_prompt_meta, repair_prompt_meta],
        "dsl_version": CONTRACT_VERSION,
        "num_ctx": args.num_ctx, "syntax_repair": bool(args.syntax_repair),
        "field_aware": bool(args.field_aware), "linker": bool(args.linker),
        "vocab_grammar": bool(args.vocab_grammar),
        "field_rows_cap": int(__import__("os").getenv("EFFECTGATE_FIELD_ROWS_CAP", "400")),
        "appworld_root": str(adapter.root), "n_tasks": len(chosen),
    })
    # Per-condition, so a second condition in the same directory cannot overwrite the
    # provenance of the first. `sanity_manifest.json` is kept as the latest-run pointer.
    (out_dir / f"sanity_manifest.{label}.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    (out_dir / "sanity_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2), flush=True)

    invalid_refs: set[str] = set()
    # The validity check is a property of AppWorld, not of a condition, so a new
    # condition directory falls back to the one the baseline measured.
    vpath = out_dir / "reference_validity_summary.json"
    if not vpath.exists():
        vpath = OUT / "reference_validity_summary.json"
    if args.skip_invalid_reference and vpath.exists():
        invalid_refs = set(json.loads(vpath.read_text()).get("invalid_task_ids") or [])
        if invalid_refs:
            print(f"[info] excluding {len(invalid_refs)} task(s) whose ground-truth "
                  f"solution fails AppWorld's own evaluator: {sorted(invalid_refs)}", flush=True)

    rows = []
    for i, t in enumerate(chosen, 1):
        tid = t["task_id"]
        if tid in invalid_refs:
            print(f"[{i}/{len(chosen)}] {tid}: skipped, reference is not known-good", flush=True)
            continue
        key = f"{label}|{args.model}|{tid}"
        if rec.done(key):
            print(f"[{i}/{len(chosen)}] {tid}: already done, skipping", flush=True)
            continue
        row: dict = {"key": key, "task_id": tid, "model": args.model,
                     "condition": label, "dsl_version": CONTRACT_VERSION,
                     "compiler_prompt": compiler_prompt_meta["path"],
                     "repair_prompt": repair_prompt_meta["path"],
                     "num_ctx": args.num_ctx, "field_aware": bool(args.field_aware),
                     "linker": bool(args.linker), "vocab_grammar": bool(args.vocab_grammar),
                     "compiler_prompt_sha256": compiler_prompt_meta["sha256"],
                     "repair_prompt_sha256": repair_prompt_meta["sha256"],
                     "instruction": t.get("instruction", "")}
        t0 = time.perf_counter()
        try:
            # 1. task + initial state + schemas
            st = adapter.get_state(tid, field_aware=args.field_aware)
            pre_view, prompt_view = st["state"], st["prompt_state"]
            # State-size accounting: tier 0 (counts + record hashes) vs tier 1 (fields).
            row["state_view_bytes"] = {
                "tier0_counts_records": len(json.dumps({"counts": pre_view["counts"],
                                                         "records": pre_view["records"]})),
                "tier1_fields": len(json.dumps(pre_view.get("fields", {}))),
                "prompt_view": len(json.dumps(prompt_view)),
            }
            if "fields_meta" in pre_view:
                fm = pre_view["fields_meta"]
                row["projection"] = {
                    "supervisor_user_ids": fm.get("supervisor_user_ids"),
                    "n_tables": len(fm.get("tables", {})),
                    "n_rows": sum(t["n_projected"] for t in fm.get("tables", {}).values()),
                    "truncated_tables": sorted(k for k, t in fm.get("tables", {}).items()
                                               if t.get("truncated")),
                }
            row["instruction"] = st["instruction"]
            row["state_fetch_s"] = st["_wall_time_s"]
            # Derived here, not sent to the model: a bare `detailed_apps` key in the
            # prompt payload was emitted as a contract path root (see prompt_state).
            row["detailed_apps"] = sorted(prompt_view.get("table_fields") or {})

            # 2. execute AppWorld's ground-truth solution FIRST. It costs ~20s
            #    against a ~300s compile, and it buys the LLM-free known-good
            #    pre-flight: a contract that rejects the state a correct execution
            #    actually produced is wrong, whatever the cause. Prompting did not
            #    fix the `forbidden` polarity error (3 of 4 contracts inverted it,
            #    the last with an explicit counter-example in the prompt), so the
            #    defect is detected mechanically instead.
            ref = adapter.reference_run(tid, field_aware=args.field_aware)

            # 3. compile a contract from the instruction + state view only
            t1 = time.perf_counter()
            # The compiler sees the summarised view; the contract is linted and
            # evaluated against the real state view (counts + records).
            out = compile_contract(
                provider, args.model, tid, st["instruction"], prompt_view,
                tool_schemas=[], prompt_path=prompt_path, format_mode="schema",
                lint_state=ref.pre_state, reference_final_state=ref.post_state,
                num_ctx=args.num_ctx,
                vocab=schema_vocab(ref.pre_state) if args.vocab_grammar else None,
            )
            row["compile_s"] = time.perf_counter() - t1
            row.update({k: v for k, v in out.summary().items()
                        if k in ("status", "n_predicates", "n_required", "n_forbidden",
                                 "n_invariants", "normalization_fixes", "error")})
            if out.llm is not None:
                calls.append(out.llm.call_record(stage="appworld_sanity", task_id=tid,
                                                parse_status=out.status))
            # 3L. v0.6 deterministic linker. Consumes the normalised compiler output
            #     whatever its status (a lint_error still has a contract_dict), and emits
            #     a bound, FK-linked, simplified, known-good-reconciled contract or fails
            #     closed. Every transformation is recorded on the row.
            row["compiled_status"] = out.status
            row["compiled_n_predicates"] = out.contract.n_predicates() if out.contract else (
                sum(len(out.contract_dict.get(k, []) or []) for k in ("required", "forbidden", "invariants"))
                if out.contract_dict else 0)
            row["compiled_contract_dict"] = out.contract_dict
            if args.linker and out.contract_dict is not None and out.status not in ("provider_error", "parse_error"):
                t3 = time.perf_counter()
                lk = link_contract(out.contract_dict, ref.pre_state, ref.post_state, ref.delta, task_id=tid)
                row["linker"] = lk.to_dict()
                row["linker_s"] = time.perf_counter() - t3
                if lk.status == "ok" and lk.contract is not None:
                    from effectgate.contracts.compiler import finalize as _finalize  # noqa: E402
                    from effectgate.providers.base import LLMResult as _LLMResult  # noqa: E402
                    linked = _finalize(tid, args.model, _LLMResult(text=json.dumps(lk.contract), metrics={}, raw={},
                                                                   ok=True, provider="linker", model="linker",
                                                                   request={}), prompt_view,
                                       reference_final_state=ref.post_state, lint_state=ref.pre_state)
                    linked.llm = out.llm
                    out = linked
                    row["status"] = out.status
                    row["n_predicates"] = out.contract.n_predicates() if out.contract else 0
                    row["error"] = out.error
                else:
                    row["status"] = f"linker_{lk.status}"
                    row["error"] = lk.error
                    out.status = f"linker_{lk.status}"
                    out.error = lk.error
                    out.contract = None

            # 3a. Mechanical repair for a contract that failed schema or static lint.
            #     Until now `--repair-rounds` fired only when a contract *compiled* and
            #     then rejected the reference state, so the dominant AppWorld failure
            #     class got zero repair attempts: in the v3 refined_v2 condition, 10 of
            #     18 tasks ended at `lint_error` with the repair budget untouched. The
            #     errors there are deterministic and specific -- "path root '/files'
            #     does not exist (known roots: counts, records)", "operator
            #     'added_count_ge' requires an integer value, got 'any value'" -- which
            #     is exactly what `repair_syntax` is for, and it needs no ground truth.
            syntax_repair = None
            if args.syntax_repair and out.status in ("lint_error", "schema_error"):
                t2 = time.perf_counter()
                fixed = repair_syntax(
                    provider, args.model, tid, st["instruction"], prompt_view,
                    tool_schemas=[], failed=out, seed=42, num_ctx=args.num_ctx,
                    lint_state=ref.pre_state, reference_final_state=ref.post_state,
                )
                syntax_repair = {"before": out.status, "after": fixed.status,
                                 "accepts_reference": fixed.accepts_reference,
                                 "wall_s": time.perf_counter() - t2,
                                 "error": (fixed.error or "")[:300]}
                if fixed.llm is not None:
                    calls.append(fixed.llm.call_record(stage="appworld_syntax_repair",
                                                       task_id=tid, parse_status=fixed.status))
                # Only adopt a repair that actually fixed the class it targets.
                if fixed.ok:
                    out = fixed
                    row["status"] = fixed.status
                    row["n_predicates"] = fixed.contract.n_predicates() if fixed.contract else 0
                    row["error"] = fixed.error
            row["syntax_repair"] = syntax_repair

            # 3b. Known-good repair. The contract rejecting the state a correct
            #     execution produced is a defect whatever its cause, and the
            #     counterexample needs no mutants: the reference state *is* it.
            #     This is the AppWorld analogue of the Phase A v2 loop.
            repair_log = []
            for rnd in range(1, args.repair_rounds + 1):
                if out.accepts_reference is not False or not out.contract:
                    break
                scored = score_contract(out.contract, ref.pre_state, ref.post_state, [], [])
                fixed = repair_contract(
                    provider, args.model, tid, st["instruction"], prompt_view,
                    tool_schemas=[], contract=out.contract,
                    counterexamples=scored.counterexamples,
                    known_good_diff=[{"table": k, **{kk: vv for kk, vv in v.items()
                                                     if kk.startswith("n_")}}
                                     for k, v in ref.delta.items()],
                    known_good=ref.post_state, lint_state=ref.pre_state,
                    prompt_path=repair_prompt,
                )
                # Every provider call is recorded, failed ones included: a repair that
                # timed out is an RQ5 cost and a taxonomy entry, not a footnote.
                if fixed.llm is not None:
                    calls.append(fixed.llm.call_record(stage="appworld_known_good_repair",
                                                       task_id=tid, parse_status=fixed.status))
                repair_log.append({"round": rnd, "status": fixed.status,
                                   "accepts_reference": fixed.accepts_reference,
                                   "n_predicates": fixed.contract.n_predicates()
                                   if fixed.contract else 0,
                                   "error": (fixed.error or "")[:300]})
                # Never let a repair make things worse.
                if fixed.ok and fixed.accepts_reference is not False:
                    out = fixed
                    break
                if fixed.ok and out.accepts_reference is False:
                    out = fixed
            row["repair_rounds_used"] = len(repair_log)
            row["repair_log"] = repair_log

            row["contract_hash"] = out.contract_hash
            row["contract_dict"] = out.contract_dict
            row["accepts_reference"] = out.accepts_reference
            row["usable"] = out.usable
            row["preflight_error"] = out.error if out.accepts_reference is False else None

            row["reference_wall_s"] = ref.wall_time_s
            row["solution_code_lines"] = ref.solution_code_lines
            row["changed_tables"] = ref.changed_tables
            row["delta"] = ref.delta
            row["pre_state_hash"] = state_hash(ref.pre_state["counts"])
            row["post_state_hash"] = state_hash(ref.post_state["counts"])
            row["execution_error"] = ref.execution_error

            # 4. official evaluator, unmodified
            row["official_success"] = ref.official_success
            row["official"] = classify_requirements(ref)
            row["official_evaluation"] = ref.official_evaluation

            # 4b. Cross-check the gold against AppWorld's *out-of-process* entry point.
            #     `world.evaluate()` runs during the session; `evaluate_task()` re-runs
            #     the same unmodified ground-truth code afterwards from the saved logs.
            #     Comparing them makes "we used the official evaluator as gold" a
            #     checked property of every row rather than a claim in the methods
            #     section, and would catch any drift introduced by our session handling.
            try:
                oop = adapter.evaluate_task(tid, experiment="effectgate_reference")
                oop_eval = oop.get("official_evaluation", {})
                row["official_success_out_of_process"] = oop_eval.get("success")
                row["official_verdict_cross_checks"] = (
                    oop_eval.get("success") == ref.official_success)
            except AppWorldError as exc:
                row["official_success_out_of_process"] = None
                row["official_verdict_cross_checks"] = None
                row["official_cross_check_error"] = str(exc)[:400]

            # 5. EffectGate contract on the same transition
            if out.ok:
                res = evaluate_contract(out.contract, ref.pre_state, ref.post_state)
                row["effectgate_pass"] = res.passed
                row["effectgate_status"] = res.status
                row["effectgate_violations"] = [o.to_dict() for o in res.violations()][:12]
                row["effectgate_errors"] = list(res.errors)[:12]
                # Representability: a predicate the state view cannot answer, as opposed
                # to a predicate that is false. Reported separately on purpose.
                row["representability_errors"] = [
                    e for e in res.errors if any(m in e for m in (
                        "truncated", "not in the field projection", "no field projection",
                        "has no field", "not typed", "is not defined"))]
                kinds = {}
                for pr in out.contract.all_predicates():
                    kinds[pr.kind] = kinds.get(pr.kind, 0) + 1
                row["predicate_kinds"] = kinds
                row["contract_hash"] = contract_hash(out.contract)
            else:
                row["effectgate_pass"] = None
                row["effectgate_status"] = out.status

            # 6. comparison, on the state requirements only
            state_ok = row["official"]["state_all_passed"]
            eg = row["effectgate_pass"]
            row["agreement"] = (
                None if (state_ok is None or eg is None)
                else ("agree" if state_ok == eg
                      else "effectgate_stricter" if state_ok and not eg
                      else "effectgate_looser")
            )
        except AppWorldError as exc:
            row["adapter_error"] = str(exc)[:1500]
        except Exception as exc:  # noqa: BLE001
            row["unexpected_error"] = f"{type(exc).__name__}: {exc}"
        row["total_s"] = time.perf_counter() - t0
        rec.append(row)
        rows.append(row)

        print(f"[{i}/{len(chosen)}] {tid}", flush=True)
        print(f"    instruction : {str(row.get('instruction'))[:96]}", flush=True)
        print(f"    contract    : {row.get('status')} preds={row.get('n_predicates')} "
              f"({row.get('compile_s', 0):.0f}s)", flush=True)
        print(f"    reference   : changed={row.get('changed_tables')} "
              f"({row.get('reference_wall_s', 0):.0f}s)", flush=True)
        off = row.get("official") or {}
        print(f"    official    : success={row.get('official_success')} "
              f"state_reqs={off.get('state_all_passed')} answer_reqs={off.get('answer_all_passed')} "
              f"cross_check={row.get('official_verdict_cross_checks')}", flush=True)
        print(f"    preflight   : accepts_known_good={row.get('accepts_reference')} "
              f"repairs={row.get('repair_rounds_used', 0)}", flush=True)
        print(f"    effectgate  : {row.get('effectgate_pass')} "
              f"({row.get('effectgate_status')})  -> {row.get('agreement')}", flush=True)
        if row.get("adapter_error") or row.get("unexpected_error"):
            print(f"    ERROR       : {row.get('adapter_error') or row.get('unexpected_error')}",
                  flush=True)

    summarise(rows, out_dir)
    return 0


def summarise(rows: list[dict], out_dir: Path) -> None:
    done = [r for r in rows if "adapter_error" not in r and "unexpected_error" not in r]
    compiled = [r for r in done if r.get("status") == "ok"]
    agree = [r for r in done if r.get("agreement") == "agree"]
    summary = {
        "n_tasks": len(rows),
        "n_completed": len(done),
        "n_contracts_compiled": len(compiled),
        "n_reference_runs_official_state_pass": sum(
            1 for r in done if (r.get("official") or {}).get("state_all_passed")),
        "official_verdict_cross_checks": {
            "matched": sum(1 for r in done if r.get("official_verdict_cross_checks") is True),
            "mismatched": sum(1 for r in done if r.get("official_verdict_cross_checks") is False),
            "unavailable": sum(1 for r in done if r.get("official_verdict_cross_checks") is None),
        },
        "agreement_counts": {
            k: sum(1 for r in done if r.get("agreement") == k)
            for k in ("agree", "effectgate_stricter", "effectgate_looser")
        },
        "n_agree": len(agree),
        "mean_compile_s": (sum(r.get("compile_s", 0) for r in compiled) / len(compiled)) if compiled else None,
        "mean_reference_s": (sum(r.get("reference_wall_s", 0) for r in done) / len(done)) if done else None,
        "errors": [r.get("adapter_error") or r.get("unexpected_error")
                   for r in rows if r.get("adapter_error") or r.get("unexpected_error")],
    }
    (out_dir / "sanity_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print("\n=== sanity summary ===")
    print(json.dumps(summary, indent=2, default=str))
    print(f"\nwrote {out_dir}/sanity_summary.json")


if __name__ == "__main__":
    sys.exit(main())
