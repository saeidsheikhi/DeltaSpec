#!/usr/bin/env python3
"""SUPPLEMENTARY ANALYSIS of the frozen DeltaSpec records (no new runs):
per-scenario aggregation and paired statistical comparisons on the same execution variants.

    python scripts/deltaspec_stats.py --out results/deltaspec_supp/stats
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from effectgate.analysis.stats import mcnemar, paired_binary_comparison, wilson  # noqa: E402

FRESH = REPO / "results" / "deltaspec_fresh"
CONDS = {"primary": ["deltaspec_fresh"],
         "primary+retry": ["deltaspec_fresh", "deltaspec_fresh_retry_transport"],
         "all": ["deltaspec_fresh", "deltaspec_fresh_retry_transport", "deltaspec_fresh_rof"]}


def load_rows(conds):
    rows = {}
    for l in (FRESH / "runs.jsonl").read_text().splitlines():
        if l.strip():
            r = json.loads(l)
            if r["condition"] in conds:
                rows[r["task_id"]] = r
    return rows


def load_oneshot(include_rof):
    out = {}
    for name in (["oneshot_on_variants.jsonl"] + (["oneshot_on_variants_rof.jsonl"] if include_rof else [])):
        p = FRESH / name
        if p.exists():
            for l in p.read_text().splitlines():
                if l.strip():
                    o = json.loads(l)
                    if o.get("status") == "ok":
                        out[o["task_id"]] = o
    return out


def w(k, n):
    p = wilson(k, n); return f"{k}/{n} = {p.p:.2f} [{p.lo:.2f}, {p.hi:.2f}]"


def variant_cases(rows):
    cases = []
    for tid, r in sorted(rows.items()):
        if r.get("status") != "ok" or r.get("official_reference") is not True:
            continue
        for v, off in r["variant_official"].items():
            if v == "reference" or off is None:
                continue
            ds = bool(r["verdicts"][v]["pass"]); oe = bool(r["baseline_output_equal"][v]); he = bool(r["baseline_hash_equal"][v])
            cases.append({"task_id": tid, "scenario": tid.rsplit("_", 1)[0], "variant": v, "official": off,
                          "deltaspec": ds == off, "output_equal": oe == off, "hash_equal": he == off,
                          "ds_pass": ds, "oe_pass": oe, "he_pass": he})
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default=str(REPO / "results" / "deltaspec_supp" / "stats"))
    args = ap.parse_args(); out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    sel = json.load(open(FRESH / "VALIDATION_SET_FROZEN.json"))
    tier = {t["task_id"]: "A" for t in sel["tier_a"]["tasks"]}; tier.update({t["task_id"]: "B" for t in sel["tier_b"]["tasks"]})
    report, data = [], {}
    for cname, conds in CONDS.items():
        rows = load_rows(conds); cases = variant_cases(rows)
        report.append(f"\n# Condition set `{cname}` ({', '.join(conds)}) — {len(rows)} rows, {len(cases)} paired variant cases\n")
        per = collections.OrderedDict()
        for tid, r in sorted(rows.items()):
            sc = tid.rsplit("_", 1)[0]; d = per.setdefault(sc, {"tier": tier.get(tid), "tasks": 0, "evaluable": 0, "auto": 0, "neg": 0, "unsafe": 0, "pos": 0, "fb": 0})
            d["tasks"] += 1
            if r.get("official_reference") is False:
                continue
            d["evaluable"] += 1
            if r.get("status") == "ok" and r.get("accepts_reference"):
                d["auto"] += 1; vo = r["variant_official"]
                d["neg"] += sum(1 for v, o in vo.items() if o is False); d["pos"] += sum(1 for v, o in vo.items() if o is True and v != "reference")
                d["unsafe"] += len(r.get("unsafe_promotions") or []); d["fb"] += len(r.get("false_block_variants") or [])
        report.append("| scenario | tier | tasks | evaluable | auto-accept | unsafe / failed variants | false blocks / passed variants |\n|---|---|---|---|---|---|---|")
        for sc, d in per.items():
            report.append(f"| {sc} | {d['tier']} | {d['tasks']} | {d['evaluable']} | {d['auto']} | {d['unsafe']}/{d['neg']} | {d['fb']}/{d['pos']} |")
        sc_eval = [d for d in per.values() if d["evaluable"]]
        sc_clean = sum(1 for d in sc_eval if d["auto"] == d["evaluable"]); sc_no_unsafe = sum(1 for d in sc_eval if d["unsafe"] == 0)
        rates = [(d["unsafe"] / d["neg"]) for d in sc_eval if d["neg"]]
        report.append(f"\n- scenarios: {len(per)}; with ≥1 evaluable task: {len(sc_eval)}; every evaluable task auto-accepted: {sc_clean}/{len(sc_eval)}; "
                      f"zero unsafe promotions: {sc_no_unsafe}/{len(sc_eval)}; mean per-scenario unsafe rate: {sum(rates)/max(1,len(rates)):.3f}")
        comp = {}
        for base in ("output_equal", "hash_equal"):
            for ck, ckname in (("task_id", "task"), ("scenario", "scenario")):
                comp[f"deltaspec_vs_{base}_by_{ckname}"] = c = paired_binary_comparison(cases, "deltaspec", base, outcome="correct", cluster_key=ck)
            m = c["mcnemar"]; bt = c["cluster_bootstrap"]
            report.append(f"- DeltaSpec vs {base} (agreement with official verdict): {w(sum(x['deltaspec'] for x in cases), len(cases))} vs {w(sum(x[base] for x in cases), len(cases))}; "
                          f"McNemar b={m['b']} c={m['c']} p={m['p_value']:.2e}; scenario-cluster bootstrap diff {bt['diff']:+.3f} [{bt['ci95'][0]:+.3f}, {bt['ci95'][1]:+.3f}]")
        neg = [x for x in cases if x["official"] is False]; pos = [x for x in cases if x["official"] is True]
        report.append(f"- officially-failed variants ({len(neg)}): accepted by DeltaSpec {sum(x['ds_pass'] for x in neg)}, by output-equality {sum(x['oe_pass'] for x in neg)}, by state-hash {sum(x['he_pass'] for x in neg)}")
        report.append(f"- officially-passed variants ({len(pos)}): blocked by DeltaSpec {sum(not x['ds_pass'] for x in pos)}, by output-equality {sum(not x['oe_pass'] for x in pos)}, by state-hash {sum(not x['he_pass'] for x in pos)}")
        mu = mcnemar([x["ds_pass"] for x in neg], [x["oe_pass"] for x in neg]); mf = mcnemar([not x["ds_pass"] for x in pos], [not x["he_pass"] for x in pos])
        report.append(f"- McNemar unsafe (DeltaSpec vs output-equality) b={mu.b} c={mu.c} p={mu.p_value:.2e}; false blocks (DeltaSpec vs state-hash) b={mf.b} c={mf.c} p={mf.p_value:.2e}")
        os_ = load_oneshot(include_rof=("deltaspec_fresh_rof" in conds))
        shared = [t for t in os_ if t in rows and rows[t].get("status") == "ok"]
        pc = []
        for t in shared:
            for v, off in os_[t]["variant_official"].items():
                if v == "reference" or off is None or v not in rows[t]["verdicts"]:
                    continue
                pc.append({"task_id": t, "scenario": t.rsplit("_", 1)[0], "official": off, "deltaspec": bool(rows[t]["verdicts"][v]["pass"]) == off, "oneshot": bool(os_[t]["verdicts"][v]) == off,
                           "ds_pass": bool(rows[t]["verdicts"][v]["pass"]), "os_pass": bool(os_[t]["verdicts"][v])})
        if pc:
            comp["deltaspec_vs_oneshot_by_task"] = c = paired_binary_comparison(pc, "deltaspec", "oneshot", outcome="correct", cluster_key="task_id")
            negp = [x for x in pc if x["official"] is False]; mu2 = mcnemar([x["ds_pass"] for x in negp], [x["os_pass"] for x in negp]); bt = c["cluster_bootstrap"]
            report.append(f"- vs v0.6.1 one-shot on {len(shared)} shared tasks / {len(pc)} variants: agreement {w(sum(x['deltaspec'] for x in pc), len(pc))} vs {w(sum(x['oneshot'] for x in pc), len(pc))}; "
                          f"unsafe on failed variants {sum(x['ds_pass'] for x in negp)} vs {sum(x['os_pass'] for x in negp)} of {len(negp)}; McNemar p={mu2.p_value:.2e}; task-cluster bootstrap diff {bt['diff']:+.3f} [{bt['ci95'][0]:+.3f}, {bt['ci95'][1]:+.3f}]")
        v061 = {}
        for l in (REPO / "results" / "appworld_linked_v061_freshB" / "sanity_runs.jsonl").read_text().splitlines():
            if l.strip():
                r = json.loads(l)
                if r["condition"] in (["linked_v061_freshB"] + (["linked_v061_freshB_rof"] if "deltaspec_fresh_rof" in conds else [])):
                    v061[r["task_id"]] = r
        ev = [t for t, r in rows.items() if r.get("official_reference") is not False]
        ds_cov = [rows[t].get("status") == "ok" and bool(rows[t].get("accepts_reference")) for t in ev]
        v_cov = [t in v061 and v061[t].get("status") == "ok" and bool(v061[t].get("accepts_reference")) for t in ev]
        mc = mcnemar(ds_cov, v_cov)
        report.append(f"- coverage on {len(ev)} evaluable tasks: DeltaSpec {w(sum(ds_cov), len(ev))} vs v0.6.1 {w(sum(v_cov), len(ev))}; McNemar b={mc.b} c={mc.c} p={mc.p_value:.2e}")
        data[cname] = {"per_scenario": per, "comparisons": comp, "n_cases": len(cases), "coverage": {"n": len(ev), "deltaspec": sum(ds_cov), "v061": sum(v_cov), "mcnemar": mc.to_dict()}}
    (out / "stats.md").write_text("\n".join(report)); (out / "stats.json").write_text(json.dumps(data, indent=2, default=str))
    print("\n".join(report)); print(f"\nwrote {out}/stats.md, stats.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
