#!/usr/bin/env python3
"""Freeze an EffectGate system version before it is run: hash every file that defines it.

A condition label identifies a *system*. This records, under `results/frozen_systems/`,
the SHA-256 of the DSL/evaluator/lint code, the environment worker, the compiler and
repair prompts, and the configuration, so that a later result can be checked against the
exact system that produced it -- and so that "we did not change the system after seeing
the result" is verifiable, not asserted.

    python scripts/freeze_system.py --name fieldaware_dsl_v0.4 --label fieldaware_v04_diag \
        --compiler-prompt prompts/contract_compiler_appworld_v4_fields.md \
        --repair-prompt prompts/contract_repair_appworld_v4_fields.md
    python scripts/freeze_system.py --verify results/frozen_systems/fieldaware_dsl_v0.4.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "results" / "frozen_systems"

SYSTEM_FILES = [
    "src/effectgate/models.py",
    "src/effectgate/contracts/fieldview.py",
    "src/effectgate/contracts/evaluator.py",
    "src/effectgate/contracts/linker.py",
    "src/effectgate/contracts/schema.py",
    "src/effectgate/contracts/io.py",
    "src/effectgate/contracts/compiler.py",
    "src/effectgate/contracts/validator.py",
    "src/effectgate/providers/ollama.py",
    "src/effectgate/adapters/appworld_adapter.py",
    "scripts/appworld_worker.py",
    "scripts/run_appworld_sanity.py",
    "schemas/effect_contract.schema.json",
    "configs/models.yaml",
    # DeltaSpec (v0.7 line): miner, labeller, assembler, synthetic mutants, driver, summary
    "src/deltaspec/__init__.py",
    "src/deltaspec/mine.py",
    "src/deltaspec/label.py",
    "src/deltaspec/assemble.py",
    "src/deltaspec/mutants.py",
    "scripts/run_deltaspec.py",
    "scripts/deltaspec_summary.py",
    "scripts/select_deltaspec_validation_set.py",
    "scripts/eval_contracts_on_variants.py",
    "scripts/worker_equivalence_check.py",
    "prompts/deltaspec_intent_labeler_v1.md",
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def snapshot(args) -> dict:
    files = {f: sha(REPO / f) for f in SYSTEM_FILES if (REPO / f).exists()}
    for f in (args.compiler_prompt, args.repair_prompt, args.labeller_prompt):
        if f:
            files[f] = sha(REPO / f)
    env = {}
    for line in (REPO / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#") and "KEY" not in line:
            k, v = line.split("=", 1); env[k] = v.strip()
    combined = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"name": args.name, "condition_labels": [args.label], "frozen_at":
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "compiler_prompt": args.compiler_prompt, "repair_prompt": args.repair_prompt,
            "labeller_prompt": args.labeller_prompt,
            "model": args.model, "num_ctx": args.num_ctx, "seed": 42, "temperature": 0.0,
            "field_rows_cap": args.field_rows_cap, "env": env,
            "combined_sha256": combined, "files": files, "note": args.note}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name"); ap.add_argument("--label")
    ap.add_argument("--compiler-prompt"); ap.add_argument("--repair-prompt")
    ap.add_argument("--labeller-prompt", help="DeltaSpec systems: the intent-labeller prompt (compiler/repair prompts then optional)")
    ap.add_argument("--model", default="phi4:latest"); ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--field-rows-cap", type=int, default=400)
    ap.add_argument("--note", default="")
    ap.add_argument("--verify", help="path of a frozen-system record to check against the tree")
    args = ap.parse_args()

    if args.verify:
        rec = json.loads(Path(args.verify).read_text())
        changed = [f for f, h in rec["files"].items() if not (REPO / f).exists() or sha(REPO / f) != h]
        if changed:
            print("CHANGED since freeze:"); [print("  ", f) for f in changed]; return 1
        print(f"UNCHANGED: {len(rec['files'])} files match {args.verify}"); return 0

    if not (args.name and args.label and (args.labeller_prompt or (args.compiler_prompt and args.repair_prompt))):
        ap.error("--name, --label and either --labeller-prompt or --compiler-prompt + --repair-prompt are required")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{args.name}.json"
    if path.exists():
        print(f"[fatal] {path} exists; a frozen system is never overwritten. Use a new --name.")
        return 2
    rec = snapshot(args)
    path.write_text(json.dumps(rec, indent=2))
    print(f"froze {len(rec['files'])} files as {args.name} (combined {rec['combined_sha256'][:16]}) -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
