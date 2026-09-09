#!/usr/bin/env python3
"""Prove AppWorld's evaluators are unmodified — as a checked property, not a promise.

MASTER_PROMPT and the Phase B task list both require that AppWorld's ground-truth
evaluators are not touched. Asserting that in prose is worth little: the whole point of
using an external benchmark is that its gold oracle is independent of us, and a reader
has no way to confirm it from a claim.

This records a SHA-256 over every task's `ground_truth/evaluation.py` and over the
`appworld` package's own evaluator modules, and compares against the stored baseline on
every later run. Any edit — ours, an upgrade's, or an accident — shows up as a named
file, not as a silently different number downstream.

    python scripts/appworld_integrity_check.py            # verify against the baseline
    python scripts/appworld_integrity_check.py --baseline # (re)create the baseline
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "results" / "appworld" / "appworld_integrity.json"

DATA_ROOT = Path(os.getenv("APPWORLD_ROOT", str(Path.home() / "appworld-root")))
PKG = REPO / ".venv-appworld" / "lib" / "python3.12" / "site-packages" / "appworld"

#: The package modules that define how evaluation happens, as opposed to the per-task
#: code that defines what is evaluated. Both must be pristine for the gold to be gold.
PKG_MODULES = ("evaluator.py", "ground_truth.py", "task.py", "environment.py")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect() -> dict[str, str]:
    files: dict[str, str] = {}
    for p in sorted((DATA_ROOT / "data" / "tasks").glob("*/ground_truth/evaluation.py")):
        files[f"data/tasks/{p.parent.parent.name}/ground_truth/evaluation.py"] = digest(p)
    for name in PKG_MODULES:
        p = PKG / name
        if p.exists():
            files[f"package/appworld/{name}"] = digest(p)
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true",
                    help="write the current hashes as the baseline (first run, or after "
                         "a deliberate AppWorld upgrade)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    out = Path(args.out)

    if not (DATA_ROOT / "data" / "tasks").is_dir():
        print(f"[fatal] AppWorld data not found at {DATA_ROOT}/data")
        return 2

    files = collect()
    rolled = hashlib.sha256(
        json.dumps(files, sort_keys=True).encode()).hexdigest()
    print(f"hashed {len(files)} files; combined digest {rolled[:16]}")

    if args.baseline or not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            {"appworld_root": str(DATA_ROOT), "n_files": len(files),
             "combined_sha256": rolled, "files": files}, indent=2), encoding="utf-8")
        print(f"wrote baseline {out}")
        return 0

    baseline = json.loads(out.read_text())
    old = baseline["files"]
    changed = sorted(k for k in set(old) & set(files) if old[k] != files[k])
    missing = sorted(set(old) - set(files))
    added = sorted(set(files) - set(old))
    if not (changed or missing or added):
        print("UNMODIFIED — every AppWorld evaluator matches the recorded baseline")
        return 0
    print("MODIFIED — AppWorld's evaluation code differs from the baseline")
    for label, items in (("changed", changed), ("missing", missing), ("added", added)):
        for k in items[:20]:
            print(f"  {label}: {k}")
        if len(items) > 20:
            print(f"  ... and {len(items) - 20} more {label}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
