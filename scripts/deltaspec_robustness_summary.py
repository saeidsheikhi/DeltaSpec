#!/usr/bin/env python3
"""Summarise the supplementary robustness/ablation run into a table (markdown + LaTeX) and figure.

    python scripts/deltaspec_robustness_summary.py --dir results/deltaspec_supp --label supp_robustness
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from effectgate.analysis.stats import wilson  # noqa: E402

ORDER = ["v1_recorded", "relabel:qwen3.5:latest", "relabel:gemma3:27b", "relabel:phi4:latest", "relabel:llama3.3:70b",
         "abl_no_exact", "abl_no_scope", "abl_no_prohib", "abl_all_req", "abl_tables"]
NAMES = {"v1_recorded": "DeltaSpec v1 (frozen contract, re-scored)", "relabel:qwen3.5:latest": "re-label: qwen3.5 (frozen labeller, determinism)",
         "relabel:gemma3:27b": "re-label: gemma3:27b", "relabel:phi4:latest": "re-label: phi4 (14B)", "relabel:llama3.3:70b": "re-label: llama3.3:70b",
         "abl_no_exact": "ablation: no exact-count facts", "abl_no_scope": "ablation: no scope invariant", "abl_no_prohib": "ablation: no prohibitions",
         "abl_all_req": "ablation: every fact required (no LLM)", "abl_tables": "ablation: table effects only (no LLM)"}


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--dir", default="results/deltaspec_supp"); ap.add_argument("--label", default="supp_robustness")
    ap.add_argument("--tex", default="paper/tables/tab_ablation.tex")
    args = ap.parse_args()
    rows = [json.loads(l) for l in (Path(args.dir) / "robustness_runs.jsonl").read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r.get("condition") == args.label and r.get("status") == "ok"]
    agg = collections.OrderedDict()
    for r in rows:
        official = r["variant_official"]
        neg = sum(1 for v, o in official.items() if o is False); pos = sum(1 for v, o in official.items() if o is True and v != "reference")
        for c, res in r["conditions"].items():
            d = agg.setdefault(c, collections.Counter())
            d["tasks"] += 1
            if res.get("status") != "ok":
                d["abstain"] += 1; continue
            if not res.get("accepts_reference"):
                d["incorrect_block"] += 1; continue
            d["auto"] += 1; d["neg"] += neg; d["pos"] += pos
            d["unsafe"] += len(res["unsafe"]); d["fb"] += len(res["false_block"])
            d["sk"] += sum(res["synthetic"].values()); d["st"] += len(res["synthetic"])
            d["same"] += bool(res.get("same_labels_as_v1")); d["label_s"] += res.get("label_s") or 0
            v1 = r["conditions"].get("v1_recorded") or {}
            if v1.get("status") == "ok":
                d["same_verdicts"] += (res["verdicts"] == v1["verdicts"])
    n = len(rows)
    def w(k, m): return f"{k}/{m} ({wilson(k, m).p:.2f})" if m else "--"
    md = [f"# Supplementary: labeller robustness and ablations — `{args.label}` — {n} tasks with a valid reference in this harness\n",
          "| condition | auto-accept | abstain / incorrect block | unsafe / failed variants | false blocks / passed variants | synthetic kill | same gate verdicts as v1 (all variants) |", "|---|---|---|---|---|---|---|"]
    tex = ["\\begin{tabular}{@{}lrrrrr@{}}", "\\toprule", "condition & auto-accept & abst.\\,/\\,inc.\\ block & unsafe / failed var. & false block / passed var. & synth.\\ kill \\\\", "\\midrule"]
    for c in [c for c in ORDER if c in agg] + [c for c in agg if c not in ORDER]:
        d = agg[c]
        md.append(f"| {NAMES.get(c, c)} | {w(d['auto'], d['tasks'])} | {d['abstain']} / {d['incorrect_block']} | {w(d['unsafe'], d['neg'])} | {w(d['fb'], d['pos'])} | {w(d['sk'], d['st'])} | {d['same_verdicts']}/{d['auto']} |")
        tex.append(f"{NAMES.get(c, c)} & {w(d['auto'], d['tasks'])} & {d['abstain']}\\,/\\,{d['incorrect_block']} & {w(d['unsafe'], d['neg'])} & {w(d['fb'], d['pos'])} & {w(d['sk'], d['st'])} \\\\")
        if c == "v1_recorded" or c.startswith("relabel:llama") or c == "relabel:phi4:latest":
            tex.append("\\midrule")
    tex += ["\\bottomrule", "\\end{tabular}"]
    (Path(args.dir) / f"summary_{args.label}.md").write_text("\n".join(md)); (Path(args.dir) / f"summary_{args.label}.json").write_text(json.dumps({"n": n, "aggregate": agg}, indent=1))
    Path(args.tex).parent.mkdir(parents=True, exist_ok=True); Path(args.tex).write_text("\n".join(tex))
    print("\n".join(md)); print(f"\nwrote {args.tex}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
