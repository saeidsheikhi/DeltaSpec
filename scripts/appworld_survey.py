#!/usr/bin/env python3
"""Survey AppWorld tasks and identify the side-effecting subset EffectGate applies to.

Runs inside the AppWorld virtualenv (it imports `appworld`), so it is invoked as:

    (cd $APPWORLD_ROOT && /path/to/.venv-appworld/bin/python scripts/appworld_survey.py \
        --splits train,dev --out /path/to/results/appworld/survey.json)

Classification is read from each task's official `evaluation_code_body` — the code
AppWorld actually runs — not guessed from the instruction text. Nothing is modified.

A task is:
  read_only       its evaluator asserts "no model changes"
  side_effecting  its evaluator asserts on changed_model_names / changed_records
  answer_only     neither (evaluator only checks the answer)

Only `side_effecting` tasks are in scope for effect contracts: a contract judges a
state transition, and has no content for "answer this and change nothing".
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

RE_NO_CHANGE = re.compile(r"no model changes", re.I)
RE_CHANGED = re.compile(r"changed_model_names|changed_records", re.I)
RE_MODELS = re.compile(r'changed_records\(\s*["\']([\w.]+)["\']')
RE_EXPECT_SET = re.compile(r"changed_model_names\([^)]*\)\s*,\s*[\"']==[\"']\s*,\s*\{([^}]*)\}", re.S)
RE_IGNORE = re.compile(r"ignore_model_names\s*=\s*\[(.*?)\]", re.S)


def classify(body: str, expected: list[str], touched: list[str]) -> str:
    """Read-only tasks *also* call `changed_model_names` -- to assert it is empty.

    So the presence of the call means nothing; what matters is whether the evaluator
    expects any model to change. "no model changes" in the requirement docstring is
    AppWorld's own marker and takes precedence.
    """
    if RE_NO_CHANGE.search(body):
        return "read_only"
    if expected or touched:
        return "side_effecting"
    if RE_CHANGED.search(body):
        # calls the machinery but expects nothing to change
        return "read_only"
    return "answer_only"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--splits", default="train,dev")
    ap.add_argument("--out", default="appworld_survey.json")
    args = ap.parse_args()

    from appworld import load_task_ids
    from appworld.task import Task

    rows = []
    for split in [s for s in args.splits.split(",") if s]:
        for tid in load_task_ids(split):
            try:
                t = Task.load(task_id=tid)
                gt = t.ground_truth
                body = getattr(gt, "evaluation_code_body", "") or ""
                touched = sorted(set(RE_MODELS.findall(body)))
                m = RE_EXPECT_SET.search(body)
                expected = sorted(
                    x.strip().strip("\"'") for x in m.group(1).split(",") if x.strip()
                ) if m else []
                mi = RE_IGNORE.search(body)
                ignored = sorted(
                    x.strip().strip("\"'") for x in mi.group(1).split(",") if x.strip()
                ) if mi else []
                kind = classify(body, expected, touched)
                rows.append({
                    "task_id": tid, "split": split, "kind": kind,
                    "instruction": t.instruction,
                    "difficulty": getattr(t, "difficulty", None),
                    "allowed_apps": list(getattr(t, "allowed_apps", []) or []),
                    "changed_records_models": touched,
                    "expected_changed_models": expected,
                    "ignored_models": ignored,
                    "n_eval_chars": len(body),
                    "n_expected_changed": len(expected),
                })
            except Exception as exc:
                rows.append({"task_id": tid, "split": split, "kind": "error",
                             "error": f"{type(exc).__name__}: {exc}"})

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")

    kinds = Counter(r["kind"] for r in rows)
    side = [r for r in rows if r["kind"] == "side_effecting"]
    print(f"scanned {len(rows)} tasks: {dict(kinds)}")
    if side:
        print(f"\nside-effecting: {len(side)} "
              f"({100*len(side)/len(rows):.0f}% of scanned)")
        print(f"  mean evaluator size: {sum(r['n_eval_chars'] for r in side)/len(side):.0f} chars")
        print(f"  mean expected changed models: "
              f"{sum(r['n_expected_changed'] for r in side)/len(side):.1f}")
        print(f"  tasks with an explicit ignore list: "
              f"{sum(1 for r in side if r['ignored_models'])}")
        print("\n  most-touched models:")
        for m, n in Counter(m for r in side for m in r["changed_records_models"]).most_common(12):
            print(f"    {m:34s} {n}")
        print("\n  simplest side-effecting tasks (fewest changed models, smallest evaluator):")
        for r in sorted(side, key=lambda r: (r["n_expected_changed"] or 99, r["n_eval_chars"]))[:10]:
            print(f"    [{r['split']}] {r['task_id']:14s} changed={r['expected_changed_models']}")
            print(f"        {r['instruction'][:100]}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
