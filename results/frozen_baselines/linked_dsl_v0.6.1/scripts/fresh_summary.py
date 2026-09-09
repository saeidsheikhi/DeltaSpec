#!/usr/bin/env python3
"""Fresh-set report for a frozen system: per-task release-gate metrics and aggregates.

Terminology (over the known-good execution, which is by construction a correct release):
  usable            a usable contract was emitted (status ok)
  auto_accept       usable AND it accepts the known-good run          (automatic coverage)
  abstain           no usable contract (fail-closed, empty, lint/provider error)
  incorrect_block   usable AND it rejects the known-good run           (false block by a wrong contract)
  agree             EffectGate verdict == official evaluator's state verdict
  unsafe_promotion  usable AND accepts a run the official evaluator fails
  effect_located    a required predicate names a table AND effect class the known-good delta exhibits

    python scripts/fresh_summary.py --dir results/appworld_linked_v061 --condition linked_v061_fresh
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from effectgate.analysis.stats import wilson  # noqa: E402
from effect_location_check import located, targets  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--condition", required=True)
    ap.add_argument("--expected", default=None, help="FRESH_SET_FROZEN.json to check coverage of the frozen list")
    args = ap.parse_args()
    rows = [json.loads(l) for l in (Path(args.dir) / "sanity_runs.jsonl").read_text().splitlines() if l.strip()]
    rows = {r["task_id"]: r for r in rows if r.get("condition") == args.condition}
    order = list(rows)
    if args.expected:
        exp = json.load(open(args.expected))["task_ids"]
        missing = [t for t in exp if t not in rows]
        order = [t for t in exp if t in rows]
        print(f"frozen list: {len(exp)} tasks; rows present: {len(order)}; missing: {missing}\n")

    def m(r):
        usable = r.get("status") == "ok"
        kg = usable and r.get("accepts_reference") is True
        off = (r.get("official") or {}).get("state_all_passed")
        eg = r.get("effectgate_pass")
        comp = r.get("compiled_contract_dict") or r.get("contract_dict") or {}
        lk = r.get("linker") if isinstance(r.get("linker"), dict) else {}
        return {
            "usable": usable, "auto_accept": kg, "abstain": not usable,
            "incorrect_block": usable and r.get("accepts_reference") is False,
            "agree": r.get("agreement") == "agree",
            "unsafe_promotion": usable and eg is True and off is False,
            "official_state_pass": off,
            "effect_located": located(targets(comp), r.get("delta") or {}),
            "compiled_status": r.get("compiled_status"), "status": r.get("status"),
            "n_tx": lk.get("n_transformations"), "preds": f"{lk.get('n_before', '–')}→{lk.get('n_after', '–')}",
            "rules": sorted({t.split(" ")[0] for t in lk.get("transformations", [])}),
            "compile_s": round(r.get("compile_s") or 0), "total_s": round(r.get("total_s") or 0),
            "abstain_reason": (r.get("error") or "")[:90] if not usable else "",
        }

    M = {t: m(rows[t]) for t in order}
    print(f"# Fresh set — `{args.condition}` — {len(order)} tasks\n")
    print("| task | official | usable | auto-accept | agree | abstain | incorrect block | unsafe | effect located | linker tx / preds | rules | compile s | total s | abstain reason |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for t in order:
        x = M[t]
        yn = lambda b: "yes" if b else "no"
        print(f"| {t} | {x['official_state_pass']} | {yn(x['usable'])} | {yn(x['auto_accept'])} | {yn(x['agree'])} | "
              f"{yn(x['abstain'])} | {yn(x['incorrect_block'])} | {yn(x['unsafe_promotion'])} | {yn(x['effect_located'])} | "
              f"{x['n_tx']} / {x['preds']} | {','.join(x['rules'])} | {x['compile_s']} | {x['total_s']} | {x['abstain_reason']} |")

    n = len(order)
    def rate(key):
        k = sum(1 for t in order if M[t][key]); w = wilson(k, n) if n else None
        return k, (f"{k}/{n} = {w.p:.2f} [{w.lo:.2f},{w.hi:.2f}]" if w else "–")
    usable_n = sum(1 for t in order if M[t]["usable"])
    prec_k = sum(1 for t in order if M[t]["usable"] and M[t]["agree"])
    agg = {k: rate(k)[1] for k in ("usable", "auto_accept", "agree", "abstain", "incorrect_block", "unsafe_promotion", "effect_located")}
    agg["precision_among_usable"] = f"{prec_k}/{usable_n}" + (f" = {prec_k/usable_n:.2f}" if usable_n else "")
    cs = [M[t]["compile_s"] for t in order]; ts = [M[t]["total_s"] for t in order]
    agg["mean_compile_s"] = round(sum(cs) / n) if n else None
    agg["mean_total_s"] = round(sum(ts) / n) if n else None
    print("\n## Aggregate\n")
    for k, v in agg.items():
        print(f"- **{k}**: {v}")
    out = Path(args.dir) / f"fresh_summary_{args.condition}.json"
    out.write_text(json.dumps({"per_task": M, "aggregate": agg, "n": n}, indent=2, default=str))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
