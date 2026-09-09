#!/usr/bin/env python3
"""Aggregate a DeltaSpec run: positive behaviour (reference), negative behaviour
(officially-failed variants and synthetic mutants), tolerance (officially-passed
variants), and the state-hash baseline on the same variants.

    python scripts/deltaspec_summary.py --dir results/deltaspec_dev --condition deltaspec_dev7
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from effectgate.analysis.stats import wilson  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True); ap.add_argument("--condition", required=True)
    ap.add_argument("--expected", default=None)
    args = ap.parse_args()
    rows = [json.loads(l) for l in (Path(args.dir) / "runs.jsonl").read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r.get("condition") == args.condition]
    if args.expected:
        exp = json.load(open(args.expected))["task_ids"]; have = {r["task_id"] for r in rows}
        print(f"frozen list {len(exp)}; rows {len(have & set(exp))}; missing {[t for t in exp if t not in have]}\n")
    n = len(rows)
    print(f"# DeltaSpec — `{args.condition}` — {n} tasks\n")
    print("| task | official ref | status | facts | required | accepts ref | variants: unsafe / false-block / killed / neg | alt (extra_read, dup) | synth kill | hash: unsafe / false-block | s |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    agg = {"usable": 0, "auto_accept": 0, "abstain": 0, "incorrect_block": 0, "invalid_ref": 0,
           "unsafe_promotions": 0, "neg_variants": 0, "killed_variants": 0, "false_block_variants": 0, "pos_variants": 0,
           "alt_extra_read": [0, 0], "alt_dup": [0, 0], "synth_killed": 0, "synth_total": 0,
           "hash_unsafe": 0, "hash_false_block": 0, "out_unsafe": 0, "out_false_block": 0,
           "oneshot_tasks": 0, "oneshot_accepts_ref": 0, "oneshot_unsafe": 0, "oneshot_neg": 0, "oneshot_false_block": 0, "oneshot_pos": 0,
           "facts": 0, "required": 0, "total_s": 0.0, "label_s": 0.0}
    for r in sorted(rows, key=lambda r: r["task_id"]):
        st = r.get("status"); vo = r.get("variant_official") or {}; vd = r.get("verdicts") or {}
        usable = st == "ok"; kg = usable and r.get("accepts_reference") is True
        agg["usable"] += usable; agg["auto_accept"] += kg
        agg["abstain"] += st in ("abstain", "adapter_error", "harness_error")
        agg["incorrect_block"] += usable and r.get("accepts_reference") is False
        agg["invalid_ref"] += st == "invalid_reference"
        neg = [v for v, o in vo.items() if o is False]; pos = [v for v, o in vo.items() if o is True and v != "reference"]
        if usable:
            agg["unsafe_promotions"] += len(r.get("unsafe_promotions") or [])
            agg["killed_variants"] += len(r.get("killed_variants") or [])
            agg["false_block_variants"] += len(r.get("false_block_variants") or [])
            agg["neg_variants"] += len(neg); agg["pos_variants"] += len(pos)
            for name, key in (("extra_read", "alt_extra_read"), ("dup_1", "alt_dup")):
                if name in vd and vo.get(name) is True:
                    agg[key][1] += 1; agg[key][0] += bool(vd[name]["pass"])
            sm = r.get("synthetic_mutants") or {}
            agg["synth_killed"] += sum(sm.values()); agg["synth_total"] += len(sm)
            hb = r.get("hash_baseline") or {}
            agg["hash_unsafe"] += len(hb.get("unsafe", [])); agg["hash_false_block"] += len(hb.get("false_block", []))
            ob = r.get("output_baseline") or {}
            agg["out_unsafe"] += len(ob.get("unsafe", [])); agg["out_false_block"] += len(ob.get("false_block", []))
        # one-shot frozen-contract baseline is scored on every task that has such a contract,
        # independent of whether DeltaSpec produced one
        os_ = r.get("oneshot_baseline")
        if isinstance(os_, dict):
            agg["oneshot_tasks"] += 1; agg["oneshot_accepts_ref"] += bool(os_.get("accepts_reference"))
            agg["oneshot_unsafe"] += len(os_.get("unsafe", [])); agg["oneshot_neg"] += len(neg)
            agg["oneshot_false_block"] += len(os_.get("false_block", [])); agg["oneshot_pos"] += len(pos)
        agg["facts"] += r.get("n_facts") or 0; agg["required"] += (r.get("assembled") or {}).get("n_required") or 0
        agg["total_s"] += r.get("total_s") or 0; agg["label_s"] += r.get("label_s") or 0
        alt = f"{vd.get('extra_read', {}).get('pass')}, {vd.get('dup_1', {}).get('pass')}" if usable else "–"
        hb = r.get("hash_baseline") or {}
        print(f"| {r['task_id']} | {r.get('official_reference')} | {st} | {r.get('n_facts')} | {(r.get('assembled') or {}).get('n_required')} | "
              f"{r.get('accepts_reference')} | {len(r.get('unsafe_promotions') or [])} / {len(r.get('false_block_variants') or [])} / "
              f"{len(r.get('killed_variants') or [])} / {len(neg)} | {alt} | {r.get('synthetic_kill_rate')} | "
              f"{len(hb.get('unsafe', []))} / {len(hb.get('false_block', []))} | {round(r.get('total_s') or 0)} |")
    def w(k, d): return f"{k}/{d} = {wilson(k, d).p:.2f} [{wilson(k, d).lo:.2f},{wilson(k, d).hi:.2f}]" if d else "–"
    print("\n## Aggregate\n")
    print(f"- usable contract: {w(agg['usable'], n)}")
    print(f"- **auto-accept known-good (automatic coverage)**: {w(agg['auto_accept'], n)}")
    print(f"- abstain: {w(agg['abstain'], n)}; incorrect block: {agg['incorrect_block']}/{n}; invalid reference: {agg['invalid_ref']}")
    print(f"- **unsafe promotions on officially-failed variants**: {agg['unsafe_promotions']}/{agg['neg_variants']}")
    print(f"- **variant kill rate** (officially-failed variants rejected): {w(agg['killed_variants'], agg['neg_variants'])}")
    print(f"- false blocks on officially-passed variants: {agg['false_block_variants']}/{agg['pos_variants']}")
    print(f"- alternative tolerance: extra_read {agg['alt_extra_read'][0]}/{agg['alt_extra_read'][1]}, duplicate-call {agg['alt_dup'][0]}/{agg['alt_dup'][1]}")
    print(f"- synthetic mutant kill rate: {w(agg['synth_killed'], agg['synth_total'])}")
    print(f"- state-hash baseline on the same variants: unsafe {agg['hash_unsafe']}/{agg['neg_variants']}, false blocks {agg['hash_false_block']}/{agg['pos_variants']}")
    print(f"- output-text baseline on the same variants: unsafe {agg['out_unsafe']}/{agg['neg_variants']}, false blocks {agg['out_false_block']}/{agg['pos_variants']}")
    if agg["oneshot_tasks"]:
        print(f"- one-shot frozen-contract baseline ({agg['oneshot_tasks']} tasks with a contract): accepts reference {agg['oneshot_accepts_ref']}/{agg['oneshot_tasks']}, "
              f"unsafe {agg['oneshot_unsafe']}/{agg['oneshot_neg']}, false blocks {agg['oneshot_false_block']}/{agg['oneshot_pos']}")
    print(f"- facts mined / required kept: {agg['facts']} / {agg['required']} (mean {agg['facts']/max(1,n):.1f} / {agg['required']/max(1,n):.1f})")
    print(f"- mean total time {agg['total_s']/max(1,n):.0f} s (labeller {agg['label_s']/max(1,n):.0f} s)")
    out = Path(args.dir) / f"summary_{args.condition}.json"
    out.write_text(json.dumps({"n": n, "aggregate": agg}, indent=2)); print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
