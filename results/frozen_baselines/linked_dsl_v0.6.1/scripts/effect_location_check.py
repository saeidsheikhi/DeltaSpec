#!/usr/bin/env python3
"""Did the compiler LOCATE the effect on the tables the known-good run actually changed?

For each row of a condition: the tables the compiler's *required* predicates target
(before linking), the tables the known-good delta shows changed, and whether they
intersect. This is the model-axis question in one number per task: a linker can bind,
link, simplify and reconcile, but it cannot move an effect to a table the compiler
never named.

    python scripts/effect_location_check.py --dir results/appworld_linked_modelaxis --condition linked_v06_dev_qwen35
    python scripts/effect_location_check.py --dir results/appworld_linked --condition linked_v06_diag
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _table(p: dict) -> str | None:
    if p.get("kind") == "record" and p.get("table"):
        return str(p["table"]).replace("/", ".")
    path = str(p.get("path") or "")
    parts = [x for x in path.split("/") if x]
    if len(parts) >= 3 and parts[0] in ("records", "counts"):
        return f"{parts[1]}.{parts[2]}"
    if len(parts) == 2 and parts[0] in ("records", "counts") and "." in parts[1]:
        return parts[1]
    return None


def _effect_class(op: str) -> str:
    if op.startswith("added_"):
        return "added"
    if op.startswith("removed_"):
        return "removed"
    if op.startswith("updated_") or op.startswith("field_") or op == "changed":
        return "updated"
    return "other"          # exists/count_*: a state property, not an effect class


def targets(contract: dict) -> set[tuple[str, str]]:
    """(table, effect class) pairs named by the required predicates."""
    out = set()
    for p in (contract or {}).get("required", []) or []:
        t = _table(p)
        if t:
            out.add((t, _effect_class(str(p.get("op", "")))))
    return out


def located(tg: set[tuple[str, str]], delta: dict) -> bool:
    """True if some required predicate names a table AND an effect class the known-good
    run actually exhibits there. Table alone is not enough: `field_transition` on a table
    whose rows were removed, or on the parent of rows that were added, is a mislocation."""
    for table, cls in tg:
        d = delta.get(table) or {}
        if cls == "added" and d.get("n_added", 0) > 0:
            return True
        if cls == "removed" and d.get("n_removed", 0) > 0:
            return True
        if cls == "updated" and d.get("n_updated", 0) > 0:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--condition", required=True)
    args = ap.parse_args()
    rows = [json.loads(l) for l in (Path(args.dir) / "sanity_runs.jsonl").read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r.get("condition") == args.condition]
    print(f"# Effect location — `{args.condition}` ({len(rows)} tasks)\n")
    print("| task | compiler (table, effect) | known-good delta | located (table+class)? | outcome | compile s |")
    print("|---|---|---|---|---|---|")
    n_loc = 0
    for r in sorted(rows, key=lambda r: r["task_id"]):
        comp = r.get("compiled_contract_dict") or r.get("contract_dict") or {}
        tg = targets(comp)
        delta = r.get("delta") or {}
        loc = located(tg, delta)
        n_loc += loc
        outcome = r.get("agreement") or r.get("status")
        kg = ", ".join(f"{t}:{'+' + str(v['n_added']) if v.get('n_added') else ''}{'-' + str(v['n_removed']) if v.get('n_removed') else ''}{'~' + str(v['n_updated']) if v.get('n_updated') else ''}"
                       for t, v in sorted(delta.items()))
        print(f"| {r['task_id']} | {', '.join(f'{t}:{c}' for t, c in sorted(tg)) or '–'} | {kg} | "
              f"{'yes' if loc else 'NO'} | {outcome} | {round(r.get('compile_s') or 0)} |")
    print(f"\neffect located (right table AND right effect class): {n_loc}/{len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
